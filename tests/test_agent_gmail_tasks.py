"""Tests for Gmail + Google Tasks agent tools.

All Google API and Muse Spark calls are mocked. No real credentials needed.
"""

import base64
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.main as main


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class FakeHttpResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeHttpClient:
    def __init__(self, handler):
        self._handler = handler
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def request(self, method, url, headers=None, **kwargs):
        self.requests.append((method, url, kwargs))
        return self._handler(method, url, kwargs)


def _stream_texts(response):
    events = []
    for frame in response.text.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


class RegistryTests(unittest.TestCase):
    def test_registry_has_all_twelve_actions(self):
        registry = main._agent_registry()
        self.assertEqual(len(registry.names()), 12)
        for name in (
            "send_email",
            "list_task_lists",
            "get_tasks",
            "get_task_details",
            "create_task",
            "update_task",
            "complete_task",
        ):
            self.assertIn(name, registry.names())

    def test_only_three_google_scopes_requested(self):
        from app.integrations.google import oauth

        self.assertIn("calendar", oauth.REQUESTED_SCOPES)
        self.assertIn("gmail.send", oauth.REQUESTED_SCOPES)
        self.assertIn(oauth.TASKS_SCOPE, oauth.REQUESTED_SCOPES)
        self.assertNotIn("spreadsheets", oauth.REQUESTED_SCOPES)
        url = oauth.build_authorize_url("cid", "http://x/cb", oauth.create_state())
        self.assertIn("include_granted_scopes=true", url)
        self.assertIn("access_type=offline", url)


class EmailValidationTests(unittest.TestCase):
    def test_recipient_subject_body_rules(self):
        from app.agent.action_models import SendEmailArgs

        ok = SendEmailArgs.model_validate(
            {"to": ["a@example.com", "b@example.com"], "subject": "Hi", "body": "Hello"}
        )
        self.assertEqual(ok.to, ["a@example.com", "b@example.com"])
        with self.assertRaises(Exception):
            SendEmailArgs.model_validate({"to": ["not-an-email"], "subject": "Hi", "body": "x"})
        with self.assertRaises(Exception):
            SendEmailArgs.model_validate({"to": [], "subject": "Hi", "body": "x"})
        with self.assertRaises(Exception):
            SendEmailArgs.model_validate({"to": ["a@example.com"], "subject": "  ", "body": "x"})
        with self.assertRaises(Exception):
            SendEmailArgs.model_validate({"to": ["a@example.com"], "subject": "Hi", "body": "  "})
        with self.assertRaises(Exception):
            SendEmailArgs.model_validate(
                {"to": ["a@example.com", "a@example.com"], "subject": "Hi", "body": "x"}
            )


class TaskValidationTests(unittest.TestCase):
    def test_task_models(self):
        from app.agent.action_models import (
            CompleteTaskArgs,
            CreateTaskArgs,
            ListTasksArgs,
            UpdateTaskArgs,
        )

        created = CreateTaskArgs.model_validate({"title": "Report", "due": "2026-11-02T10:00:00+05:30"})
        self.assertEqual(created.task_list_id, "@default")
        self.assertIsNotNone(created.due)
        with self.assertRaises(Exception):
            CreateTaskArgs.model_validate({"title": "   "})
        with self.assertRaises(Exception):
            ListTasksArgs.model_validate({"max_results": 500})
        with self.assertRaises(Exception):
            UpdateTaskArgs.model_validate({"task_id": "t1"})
        updated = UpdateTaskArgs.model_validate({"task_id": "t1", "status": "completed"})
        self.assertEqual(updated.status, "completed")
        with self.assertRaises(Exception):
            UpdateTaskArgs.model_validate({"task_id": "t1", "status": "bogus"})
        CompleteTaskArgs.model_validate({"task_id": "t1"})


class RecipientMatchingTests(unittest.TestCase):
    def test_recipient_order_and_shape_do_not_matter(self):
        from app.agent.confirmation import matching_details_for

        stored = {"to": ["b@example.com", "a@example.com"], "subject": "Hi", "body": "x"}
        incoming = {"to": ["a@example.com", "b@example.com"], "subject": "Hi", "body": "x"}
        self.assertTrue(matching_details_for("send_email", stored, incoming))
        as_string = {"to": "a@example.com, b@example.com", "subject": "Hi", "body": "x"}
        self.assertTrue(matching_details_for("send_email", stored, as_string))
        different = {"to": ["a@example.com", "c@example.com"], "subject": "Hi", "body": "x"}
        self.assertFalse(matching_details_for("send_email", stored, different))


class AffirmativeConfirmationTests(unittest.TestCase):
    def test_affirmative_wording_counts_without_new_details(self):
        from app.agent.confirmation import is_affirmative_confirmation

        for text in ("Yes, send it.", "yes", "Yeah, schedule it", "Confirm.", "ok, do it"):
            self.assertTrue(is_affirmative_confirmation(text), text)

    def test_messages_with_new_details_do_not_count(self):
        from app.agent.confirmation import is_affirmative_confirmation

        for text in ("Yes, make it 3pm", "john@example.com", "yes, tomorrow at 10",
                     "confirm, send it to b@example.com instead"):
            self.assertFalse(is_affirmative_confirmation(text), text)


class EmailDraftFlowTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(main, "_google_connect_message", return_value=None)
        self.addCleanup(patcher.stop)
        patcher.start()

        async def fake_draft(post_chat, *, purpose, subject="", to=()):
            return {"subject": subject, "body": purpose}

        draft_patcher = patch("app.agent.mail_drafter.draft_email", fake_draft)
        self.addCleanup(draft_patcher.stop)
        draft_patcher.start()
    """Full slot-filling run: recipient -> subject -> body -> proposal -> send."""

    def _scripted_client(self, selections):
        import os

        calls = {"n": 0}

        async def fake_select(post_chat, messages, label):
            selection = selections[min(calls["n"], len(selections) - 1)]
            calls["n"] += 1
            return selection

        async def fake_chat(messages, _api_key):
            yield main._sse_event({"type": "delta", "text": "Chatting."})
            yield main._sse_event({"type": "done"})

        return patch.dict(os.environ, {"MODEL_API_KEY": "test-key"}), fake_select, fake_chat

    def _texts(self, response):
        events = []
        for frame in response.text.split("\n\n"):
            for line in frame.splitlines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
        return " ".join(event.get("text", "") for event in events)

    def _post(self, client, text, session):
        return client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": text}], "session_id": session},
        )

    def test_bare_send_intent_skips_selector(self):
        async def exploding_select(post_chat, messages, label):
            raise AssertionError("selector should be skipped on the fast path")

        with patch.dict("os.environ", {"MODEL_API_KEY": "test-key"}):
            with patch("app.agent.selector.select_action", exploding_select):
                with TestClient(main.app) as client:
                    first = self._post(client, "I want to send a mail", "mail-fast")
        self.assertIn("Who should I send it to", self._texts(first))

    def test_detailed_send_still_uses_selector(self):
        calls = {"n": 0}

        async def counting_select(post_chat, messages, label):
            calls["n"] += 1
            return {"action": "send_email",
                    "arguments": {"to": ["john@example.com"],
                                  "subject": "Hi", "body": "Yo"}}

        from app.agent.tool_executor import ToolResult

        async def fake_execute(registry, action, args, context):
            return ToolResult(ok=False, code="needs_confirmation",
                              message="Proposal.", needs_confirmation=True,
                              proposal="Please confirm the email.",
                              arguments=args)

        with patch.dict("os.environ", {"MODEL_API_KEY": "test-key"}):
            with patch("app.agent.selector.select_action", counting_select):
                with patch("app.agent.tool_executor.execute_tool", fake_execute):
                    with TestClient(main.app) as client:
                        first = self._post(client, "Send a mail to john@example.com", "mail-detail")
        self.assertEqual(calls["n"], 1)
        self.assertIn("What subject", self._texts(first))

    def test_subject_and_message_in_one_go_splits(self):
        env, fake_select, fake_chat = self._scripted_client([
            {"action": "none"},  # address turn; selector misses it
            {"action": "none"},  # one-go turn; splitter handles it
        ])
        from app.agent.tool_executor import ToolResult

        async def fake_execute(registry, action, args, context):
            if not context.get("confirmed"):
                proposal = (
                    f"Here is the email: To: {', '.join(args.get('to', []))}. "
                    f"Subject: {args.get('subject', '')}. Body: {args.get('body', '')} "
                    f"Reply with 'Confirm'."
                )
                return ToolResult(ok=False, code="needs_confirmation",
                                  message=proposal, needs_confirmation=True,
                                  proposal=proposal, arguments=args)
            return ToolResult(ok=True, code="ok", message="sent", data={"id": "m1"})

        async def fake_draft(post_chat, *, purpose, subject="", to=()):
            return {"subject": subject or "Hi", "body": f"Drafted: {purpose}"}

        with env:
            with patch("app.agent.selector.select_action", fake_select):
                with patch("app.agent.tool_executor.execute_tool", fake_execute):
                    with patch("app.agent.mail_drafter.draft_email", fake_draft):
                        with patch.object(main, "_stream_muse_chat", fake_chat):
                            with TestClient(main.app) as client:
                                first = self._post(client, "I want to send a mail", "mail-onego")
                                self.assertIn("Who should I send it to", self._texts(first))
                                second = self._post(client, "shantan1213@gmail.com", "mail-onego")
                                self.assertIn("What subject", self._texts(second))
                                third = self._post(client, "test mail and I want to say hi how are you and let's meet tomorrow", "mail-onego")
                                proposal = self._texts(third)
                                self.assertIn("Subject: test mail.", proposal)
                                self.assertIn("Drafted: hi how are you and let's meet tomorrow", proposal)
                                self.assertNotIn("I want to", proposal)

    def test_recipient_subject_body_then_send(self):
        env, fake_select, fake_chat = self._scripted_client([
            {"action": "send_email", "arguments": {}},  # "I want to send a mail"
            {"action": "none"},  # bare typed address; selector misses it
            {"action": "none"},  # "test mail"
            {"action": "none"},  # "hi how are you"
        ])
        from app.agent.tool_executor import ToolResult

        async def fake_execute(registry, action, args, context):
            if not context.get("confirmed"):
                proposal = (
                    f"Here is the email: To: {', '.join(args.get('to', []))}. "
                    f"Subject: {args.get('subject', '')}. Body: {args.get('body', '')} "
                    f"Reply with 'Confirm'."
                )
                return ToolResult(ok=False, code="needs_confirmation",
                                  message=proposal, needs_confirmation=True,
                                  proposal=proposal, arguments=args)
            return ToolResult(ok=True, code="ok", message="sent", data={"id": "m1"})

        async def fake_draft(post_chat, *, purpose, subject="", to=()):
            return {"subject": subject or "Hi", "body": f"Drafted: {purpose}"}

        with env:
            with patch("app.agent.selector.select_action", fake_select):
                with patch("app.agent.tool_executor.execute_tool", fake_execute):
                    with patch("app.agent.mail_drafter.draft_email", fake_draft):
                        with patch.object(main, "_stream_muse_chat", fake_chat):
                            with TestClient(main.app) as client:
                                first = self._post(client, "I want to send a mail", "mail-draft")
                                self.assertIn("Who should I send it to", self._texts(first))
                                second = self._post(client, "shantan1213@gmail.com", "mail-draft")
                                self.assertIn("What subject", self._texts(second))
                                third = self._post(client, "test mail", "mail-draft")
                                self.assertIn("What should the message say", self._texts(third))
                                fourth = self._post(client, "hi how are you", "mail-draft")
                                proposal = self._texts(fourth)
                                self.assertIn("shantan1213@gmail.com", proposal)
                                self.assertIn("test mail", proposal)
                                self.assertIn("Drafted: hi how are you", proposal)
                                fifth = self._post(client, "Yes, send it.", "mail-draft")
                                self.assertIn("sent successfully", self._texts(fifth))

    def test_selector_supplied_body_is_ai_drafted_before_send(self):
        env, fake_select, fake_chat = self._scripted_client([
            {"action": "send_email", "arguments": {}},
            {"action": "none"},  # "test mail"
            {"action": "send_email",
             "arguments": {"to": ["shantan1213@gmail.com"],
                           "subject": "test mail",
                           "body": "uh just say hi how are you"}},
        ])
        from app.agent.tool_executor import ToolResult

        async def fake_execute(registry, action, args, context):
            proposal = (
                f"Here is the email: To: {', '.join(args.get('to', []))}. "
                f"Subject: {args.get('subject', '')}. Body: {args.get('body', '')} "
                f"Reply with 'Confirm'."
            )
            if not context.get("confirmed"):
                return ToolResult(ok=False, code="needs_confirmation",
                                  message=proposal, needs_confirmation=True,
                                  proposal=proposal, arguments=args)
            return ToolResult(ok=True, code="ok", message="sent", data={"id": "m1"})

        async def fake_draft(post_chat, *, purpose, subject="", to=()):
            return {"subject": subject or "Hi", "body": f"Drafted: {purpose}"}

        with env:
            with patch("app.agent.selector.select_action", fake_select):
                with patch("app.agent.tool_executor.execute_tool", fake_execute):
                    with patch("app.agent.mail_drafter.draft_email", fake_draft):
                        with patch.object(main, "_stream_muse_chat", fake_chat):
                            with TestClient(main.app) as client:
                                self._post(client, "I want to send a mail", "mail-selector-body")
                                self._post(client, "shantan1213@gmail.com", "mail-selector-body")
                                self._post(client, "test mail", "mail-selector-body")
                                fourth = self._post(client, "uh just say hi how are you", "mail-selector-body")
                                proposal = self._texts(fourth)
                                self.assertIn("Drafted: uh just say hi how are you", proposal)
                                self.assertNotIn("Body: uh just say hi how are you", proposal)

    def test_body_falls_back_to_raw_words_when_drafter_fails(self):
        env, fake_select, fake_chat = self._scripted_client([
            {"action": "send_email", "arguments": {}},
            {"action": "none"},  # bare typed address; selector misses it
            {"action": "none"},  # "test mail"
            {"action": "none"},  # "hi how are you"
        ])
        from app.agent.tool_executor import ToolResult

        async def fake_execute(registry, action, args, context):
            if not context.get("confirmed"):
                proposal = (
                    f"Here is the email: To: {', '.join(args.get('to', []))}. "
                    f"Subject: {args.get('subject', '')}. Body: {args.get('body', '')} "
                    f"Reply with 'Confirm'."
                )
                return ToolResult(ok=False, code="needs_confirmation",
                                  message=proposal, needs_confirmation=True,
                                  proposal=proposal, arguments=args)
            return ToolResult(ok=True, code="ok", message="sent", data={"id": "m1"})

        async def broken_draft(post_chat, *, purpose, subject="", to=()):
            raise RuntimeError("drafter down")

        with env:
            with patch("app.agent.selector.select_action", fake_select):
                with patch("app.agent.tool_executor.execute_tool", fake_execute):
                    with patch("app.agent.mail_drafter.draft_email", broken_draft):
                        with patch.object(main, "_stream_muse_chat", fake_chat):
                            with TestClient(main.app) as client:
                                self._post(client, "I want to send a mail", "mail-fallback")
                                self._post(client, "shantan1213@gmail.com", "mail-fallback")
                                self._post(client, "test mail", "mail-fallback")
                                fourth = self._post(client, "hi how are you", "mail-fallback")
                                proposal = self._texts(fourth)
                                self.assertIn("Body: hi how are you", proposal)

    def test_cancel_drops_the_draft(self):
        env, fake_select, fake_chat = self._scripted_client([
            {"action": "send_email", "arguments": {}},
            {"action": "none"},
        ])
        with env:
            with patch("app.agent.selector.select_action", fake_select):
                with patch.object(main, "_stream_muse_chat", fake_chat):
                    with TestClient(main.app) as client:
                        self._post(client, "I want to send a mail", "mail-cancel")
                        cancelled = self._post(client, "cancel that", "mail-cancel")
                        self.assertIn("dropped", self._texts(cancelled).lower())
                        # Draft is gone: an unrelated short message chats normally.
                        after = self._post(client, "test mail", "mail-cancel")
                        self.assertIn("Chatting.", self._texts(after))

    def test_draft_store_expiry(self):
        from app.agent.drafts import EmailDraftStore

        store = EmailDraftStore()
        store.save("s", {"to": ["a@example.com"]}, "subject")
        self.assertIsNotNone(store.get_valid("s"))
        store._drafts["s"]["updated_at"] -= 700
        self.assertIsNone(store.get_valid("s"))


class ScopeTests(unittest.TestCase):
    def test_missing_scope_guidance_and_granted_scopes(self):
        from pathlib import Path

        from app.integrations.google import token_store

        self.assertEqual(token_store.granted_scopes(Path("/nonexistent/path.json")), set())
        with unittest.mock.patch.object(
            token_store, "load_tokens", return_value={"refresh_token": "r", "scope": "a b"}
        ):
            self.assertEqual(token_store.granted_scopes("x"), {"a", "b"})

    def test_gmail_forbidden_maps_to_reconnect_message(self):
        import asyncio

        from app.integrations.google import gmail as gmail_module

        async def ensure_token(force_refresh=False):
            return "tok"

        def handler(method, url, kwargs):
            return FakeHttpResponse(403, {})

        with self.assertRaises(PermissionError) as ctx:
            asyncio.new_event_loop().run_until_complete(
                gmail_module.send_email_message(
                    FakeHttpClient(handler), ensure_token, ["a@example.com"], "Hi", "Body"
                )
            )
        self.assertIn("/auth/google/login", str(ctx.exception))

    def test_tasks_404_surfaces_not_found(self):
        import asyncio

        from app.integrations.google import tasks as tasks_module

        async def ensure_token(force_refresh=False):
            return "tok"

        def handler(method, url, kwargs):
            return FakeHttpResponse(404, {})

        with self.assertRaises(Exception) as ctx:
            asyncio.new_event_loop().run_until_complete(
                tasks_module.get_task_details(FakeHttpClient(handler), ensure_token, "@default", "nope")
            )
        self.assertEqual(ctx.exception.code, "not_found")


class GmailServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_message_is_valid_mime(self):
        from app.integrations.google import gmail as gmail_module

        raw = gmail_module.build_raw_message(["a@example.com"], "Subject here", "Body text")
        decoded = base64.urlsafe_b64decode(raw).decode()
        self.assertIn("a@example.com", decoded)
        self.assertIn("Subject here", decoded)
        self.assertIn("Body text", decoded)

    async def test_send_posts_to_gmail_api(self):
        from app.integrations.google import gmail as gmail_module

        async def ensure_token(force_refresh=False):
            return "tok"

        def handler(method, url, kwargs):
            self.assertEqual(method, "POST")
            self.assertTrue(url.endswith("/users/me/messages/send"))
            self.assertIn("raw", kwargs["json"])
            return FakeHttpResponse(200, {"id": "m1", "threadId": "t1"})

        sent = await gmail_module.send_email_message(
            FakeHttpClient(handler), ensure_token, ["a@example.com"], "Hi", "Body"
        )
        self.assertEqual(sent["id"], "m1")
        self.assertEqual(sent["subject"], "Hi")


class TasksServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_lists_and_crud(self):
        from app.integrations.google import tasks as tasks_module

        async def ensure_token(force_refresh=False):
            return "tok"

        def lists_handler(method, url, kwargs):
            self.assertTrue(url.endswith("/users/@me/lists"))
            return FakeHttpResponse(200, {"items": [{"id": "L1", "title": "My Tasks"}]})

        result = await tasks_module.list_task_lists(FakeHttpClient(lists_handler), ensure_token)
        self.assertEqual(result["lists"], [{"id": "L1", "title": "My Tasks"}])

        def tasks_handler(method, url, kwargs):
            self.assertIn("/lists/L1/tasks", url)
            return FakeHttpResponse(200, {"items": [
                {"id": "T1", "title": "Pay bill", "status": "needsAction"},
                {"id": "T2", "title": "Done thing", "status": "completed"},
            ]})

        result = await tasks_module.get_tasks(
            FakeHttpClient(tasks_handler), ensure_token, "L1", 20, False
        )
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["pending_count"], 1)

        def create_handler(method, url, kwargs):
            self.assertEqual(method, "POST")
            self.assertEqual(kwargs["json"]["title"], "New task")
            return FakeHttpResponse(200, {"id": "T3", "title": "New task", "status": "needsAction"})

        created = await tasks_module.create_task(
            FakeHttpClient(create_handler), ensure_token, "@default", "New task"
        )
        self.assertEqual(created["id"], "T3")

        def patch_handler(method, url, kwargs):
            self.assertEqual(method, "PATCH")
            self.assertEqual(kwargs["json"]["status"], "completed")
            return FakeHttpResponse(200, {"id": "T1", "title": "Pay bill", "status": "completed"})

        done = await tasks_module.complete_task(
            FakeHttpClient(patch_handler), ensure_token, "@default", "T1"
        )
        self.assertEqual(done["status"], "completed")


class EmailConfirmationFlowTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(main, "_google_connect_message", return_value=None)
        self.addCleanup(patcher.stop)
        patcher.start()

        async def fake_draft(post_chat, *, purpose, subject="", to=()):
            return {"subject": subject, "body": purpose}

        draft_patcher = patch("app.agent.mail_drafter.draft_email", fake_draft)
        self.addCleanup(draft_patcher.stop)
        draft_patcher.start()
    def test_send_requires_confirm_and_single_use(self):
        import os

        async def fake_select(post_chat, messages, label):
            return {"action": "send_email",
                    "arguments": {"to": ["john@example.com"],
                                  "subject": "Meeting",
                                  "body": "I will join tomorrow."}}

        from app.agent.tool_executor import ToolResult

        async def fake_execute(registry, action, args, context):
            if not context.get("confirmed"):
                return ToolResult(ok=False, code="needs_confirmation",
                                  message="Here is the email.",
                                  needs_confirmation=True, proposal="Here is the email.",
                                  arguments=args)
            return ToolResult(ok=True, code="ok", message="sent",
                              data={"id": "m1", "subject": args["subject"]})

        async def fake_chat(messages, _api_key):
            yield main._sse_event({"type": "delta", "text": "Chatting."})
            yield main._sse_event({"type": "done"})

        with patch.dict(os.environ, {"MODEL_API_KEY": "test-key"}):
            with patch("app.agent.selector.select_action", fake_select):
                with patch("app.agent.tool_executor.execute_tool", fake_execute):
                    with patch.object(main, "_stream_muse_chat", fake_chat):
                        with TestClient(main.app) as client:
                            first = client.post(
                                "/api/chat/stream",
                                json={"messages": [{"role": "user",
                                                    "content": "Send an email to john@example.com"}],
                                      "session_id": "mail-1"},
                            )
                            self.assertIn("email", first.text.lower())
                            second = client.post(
                                "/api/chat/stream",
                                json={"messages": [{"role": "user", "content": "Confirm."}],
                                      "session_id": "mail-1"},
                            )
                            self.assertIn("sent successfully", second.text)
                            third = client.post(
                                "/api/chat/stream",
                                json={"messages": [{"role": "user", "content": "Confirm."}],
                                      "session_id": "mail-1"},
                            )
                            self.assertNotIn("sent successfully", third.text)

    def test_confirmed_send_streams_progress_before_success(self):
        import json
        import os

        async def fake_select(post_chat, messages, label):
            return {"action": "send_email",
                    "arguments": {"to": ["john@example.com"],
                                  "subject": "Meeting",
                                  "body": "I will join tomorrow."}}

        from app.agent.tool_executor import ToolResult

        async def fake_execute(registry, action, args, context):
            if not context.get("confirmed"):
                return ToolResult(ok=False, code="needs_confirmation",
                                  message="Here is the email.",
                                  needs_confirmation=True, proposal="Here is the email.",
                                  arguments=args)
            return ToolResult(ok=True, code="ok", message="sent",
                              data={"id": "m1", "subject": args["subject"]})

        with patch.dict(os.environ, {"MODEL_API_KEY": "test-key"}):
            with patch("app.agent.selector.select_action", fake_select):
                with patch("app.agent.tool_executor.execute_tool", fake_execute):
                    with TestClient(main.app) as client:
                        client.post(
                            "/api/chat/stream",
                            json={"messages": [{"role": "user",
                                                "content": "Send an email to john@example.com"}],
                                  "session_id": "mail-progress"},
                        )
                        second = client.post(
                            "/api/chat/stream",
                            json={"messages": [{"role": "user", "content": "Confirm."}],
                                  "session_id": "mail-progress"},
                        )
        texts = []
        for frame in second.text.split("\n\n"):
            for line in frame.splitlines():
                if line.startswith("data:"):
                    event = json.loads(line[5:].strip())
                    if event.get("type") == "delta":
                        texts.append(event.get("text", ""))
        self.assertGreaterEqual(len(texts), 2)
        self.assertIn("Sending your mail now", texts[0])
        self.assertTrue(any("sent successfully" in text for text in texts[1:]))

    def test_yes_send_it_confirms_despite_incomplete_reselection(self):
        import os

        calls = {"n": 0}

        async def fake_select(post_chat, messages, label):
            # Turn 1 proposes from complete args; turn 2 ("Yes, send it.")
            # reselects with no usable details.
            calls["n"] += 1
            if calls["n"] == 1:
                return {"action": "send_email",
                        "arguments": {"to": ["john@example.com"],
                                      "subject": "Meeting", "body": "Hi"}}
            return {"action": "send_email", "arguments": {}}

        from app.agent.tool_executor import ToolResult

        async def fake_execute(registry, action, args, context):
            if not context.get("confirmed"):
                return ToolResult(ok=False, code="needs_confirmation",
                                  message="Here is the email.",
                                  needs_confirmation=True, proposal="Here is the email.",
                                  arguments={"to": ["john@example.com"],
                                             "subject": "Meeting", "body": "Hi"})
            return ToolResult(ok=True, code="ok", message="sent",
                              data={"id": "m1", "subject": args["subject"]})

        async def fake_chat(messages, _api_key):
            yield main._sse_event({"type": "delta", "text": "Chatting."})
            yield main._sse_event({"type": "done"})

        with patch.dict(os.environ, {"MODEL_API_KEY": "test-key"}):
            with patch("app.agent.selector.select_action", fake_select):
                with patch("app.agent.tool_executor.execute_tool", fake_execute):
                    with patch.object(main, "_stream_muse_chat", fake_chat):
                        with TestClient(main.app) as client:
                            first = client.post(
                                "/api/chat/stream",
                                json={"messages": [{"role": "user",
                                                    "content": "Send an email to john@example.com"}],
                                      "session_id": "mail-yes"},
                            )
                            self.assertIn("email", first.text.lower())
                            second = client.post(
                                "/api/chat/stream",
                                json={"messages": [{"role": "user", "content": "Yes, send it."}],
                                      "session_id": "mail-yes"},
                            )
                            self.assertIn("sent successfully", second.text)

    def test_yes_with_new_time_does_not_confirm(self):
        import os

        async def fake_select(post_chat, messages, label):
            return {"action": "send_email",
                    "arguments": {"to": ["john@example.com"],
                                  "subject": "Meeting", "body": "Hi"}}

        async def fake_chat(messages, _api_key):
            yield main._sse_event({"type": "delta", "text": "Chatting."})
            yield main._sse_event({"type": "done"})

        from app.agent.tool_executor import ToolResult

        async def fake_execute(registry, action, args, context):
            if not context.get("confirmed"):
                return ToolResult(ok=False, code="needs_confirmation",
                                  message="Here is the email.",
                                  needs_confirmation=True, proposal="Here is the email.",
                                  arguments={"to": ["john@example.com"],
                                             "subject": "Meeting", "body": "Hi"})
            return ToolResult(ok=True, code="ok", message="sent", data={"id": "m1"})

        with patch.dict(os.environ, {"MODEL_API_KEY": "test-key"}):
            with patch("app.agent.selector.select_action", fake_select):
                with patch("app.agent.tool_executor.execute_tool", fake_execute):
                    with patch.object(main, "_stream_muse_chat", fake_chat):
                        with TestClient(main.app) as client:
                            first = client.post(
                                "/api/chat/stream",
                                json={"messages": [{"role": "user",
                                                    "content": "Send an email to john@example.com"}],
                                      "session_id": "mail-time"},
                            )
                            self.assertIn("email", first.text.lower())
                            second = client.post(
                                "/api/chat/stream",
                                json={"messages": [{"role": "user",
                                                    "content": "Yes, make it 3pm"}],
                                      "session_id": "mail-time"},
                            )
                            self.assertNotIn("sent successfully", second.text)

    def test_changed_details_discard_pending(self):
        import os

        calls = {"n": 0}

        async def fake_select(post_chat, messages, label):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"action": "send_email",
                        "arguments": {"to": ["a@example.com"], "subject": "One", "body": "First"}}
            return {"action": "send_email",
                    "arguments": {"to": ["b@example.com"], "subject": "Two", "body": "Second"}}

        from app.agent.tool_executor import ToolResult

        async def fake_execute(registry, action, args, context):
            if not context.get("confirmed"):
                return ToolResult(ok=False, code="needs_confirmation",
                                  message="Proposal.", needs_confirmation=True,
                                  proposal="Proposal.", arguments=args)
            return ToolResult(ok=True, code="ok", message="sent", data={"id": "m9"})

        with patch.dict(os.environ, {"MODEL_API_KEY": "test-key"}):
            with patch("app.agent.selector.select_action", fake_select):
                with patch("app.agent.tool_executor.execute_tool", fake_execute):
                    with TestClient(main.app) as client:
                        client.post(
                            "/api/chat/stream",
                            json={"messages": [{"role": "user", "content": "Send email to a@"}],
                                  "session_id": "mail-2"},
                        )
                        # Different recipient + explicit confirm word: must NOT send the old mail.
                        second = client.post(
                            "/api/chat/stream",
                            json={"messages": [{"role": "user",
                                                "content": "Confirm, send it to b@example.com instead"}],
                                  "session_id": "mail-2"},
                        )
                        self.assertNotIn("sent successfully", second.text)


class TasksFlowTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(main, "_google_connect_message", return_value=None)
        self.addCleanup(patcher.stop)
        patcher.start()
    def test_create_task_confirmation(self):
        import os

        async def fake_select(post_chat, messages, label):
            return {"action": "create_task",
                    "arguments": {"title": "Submit report",
                                  "due": "2026-11-03T18:00:00+05:30", "notes": ""}}

        from app.agent.tool_executor import ToolResult

        async def fake_execute(registry, action, args, context):
            if not context.get("confirmed"):
                return ToolResult(ok=False, code="needs_confirmation",
                                  message="Should I add it? Reply with 'Confirm'.",
                                  needs_confirmation=True,
                                  proposal="Should I add it? Reply with 'Confirm'.",
                                  arguments=args)
            return ToolResult(ok=True, code="ok", message="created",
                              data={"id": "T9", "title": args["title"]})

        async def fake_chat(messages, _api_key):
            yield main._sse_event({"type": "delta", "text": "Chatting."})
            yield main._sse_event({"type": "done"})

        with patch.dict(os.environ, {"MODEL_API_KEY": "test-key"}):
            with patch("app.agent.selector.select_action", fake_select):
                with patch("app.agent.tool_executor.execute_tool", fake_execute):
                    with patch.object(main, "_stream_muse_chat", fake_chat):
                        with TestClient(main.app) as client:
                            first = client.post(
                                "/api/chat/stream",
                                json={"messages": [{"role": "user",
                                                    "content": "Remind me to submit my report"}],
                                      "session_id": "task-1"},
                            )
                            self.assertIn("Confirm", first.text)
                            second = client.post(
                                "/api/chat/stream",
                                json={"messages": [{"role": "user", "content": "Confirm."}],
                                      "session_id": "task-1"},
                            )
                            self.assertIn("added", second.text)

    def test_read_inbox_is_unsupported(self):
        import os

        async def fake_select(post_chat, messages, label):
            return {"action": "read_inbox", "arguments": {}}

        with patch.dict(os.environ, {"MODEL_API_KEY": "test-key"}):
            with patch("app.agent.selector.select_action", fake_select):
                with TestClient(main.app) as client:
                    response = client.post(
                        "/api/chat/stream",
                        json={"messages": [{"role": "user",
                                            "content": "Read my latest email"}],
                              "session_id": "mail-3"},
                    )
        texts = " ".join(
            e.get("text", "") for e in _stream_texts(response) if isinstance(e, dict)
        )
        self.assertIn("can't do that yet", texts)


class ToolKeywordsTests(unittest.TestCase):
    def test_gmail_task_keywords_gate(self):
        from app.agent import selector

        self.assertTrue(selector.looks_tool_related("Send an email to John"))
        self.assertTrue(selector.looks_tool_related("Draft an email"))
        self.assertTrue(selector.looks_tool_related("Compose a message"))
        self.assertTrue(selector.looks_tool_related("Schedule a meeting"))
        self.assertTrue(selector.looks_tool_related("Show my pending tasks"))
        self.assertTrue(selector.looks_tool_related("Remind me to pay the bill"))
        self.assertFalse(selector.looks_tool_related("Tell me a joke"))


if __name__ == "__main__":
    unittest.main()
