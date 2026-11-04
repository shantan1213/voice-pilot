"""Tests for the Raala agent-tool framework and Google Calendar tool.

All Google API and Muse Spark calls are mocked. No real credentials needed.
"""

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.main as main


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class RegistryTests(unittest.TestCase):
    def test_calendar_registers_exactly_five_actions(self):
        registry = main._agent_registry()
        self.assertEqual(
            registry.names(),
            [
                "check_calendar_availability",
                "complete_task",
                "create_calendar_event",
                "create_task",
                "get_event_details",
                "get_task_details",
                "get_tasks",
                "get_today_events",
                "get_upcoming_events",
                "list_task_lists",
                "send_email",
                "update_task",
            ],
        )

    def test_openai_tool_definitions_have_valid_schemas(self):
        registry = main._agent_registry()
        tools = registry.openai_tools()
        self.assertEqual(len(tools), 12)
        for tool in tools:
            self.assertEqual(tool["type"], "function")
            self.assertIn("name", tool["function"])
            self.assertIn("parameters", tool["function"])


class SelectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_keyword_prefilter(self):
        from app.agent import selector

        self.assertTrue(selector.looks_calendar_related("What meetings do I have today?"))
        self.assertTrue(selector.looks_calendar_related("Am I free tomorrow?"))
        self.assertFalse(selector.looks_calendar_related("Tell me a joke"))
        self.assertFalse(selector.looks_calendar_related("What is the capital of France?"))

    async def test_parses_native_tool_call(self):
        from app.agent import selector

        async def fake_post(payload):
            self.assertNotIn("tools", payload)
            self.assertNotIn("tool_choice", payload)
            self.assertEqual(payload["max_completion_tokens"], 1024)
            return {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "get_today_events",
                                        "arguments": "{}",
                                    }
                                }
                            ]
                        }
                    }
                ]
            }

        selection = await selector.select_action(fake_post, [], "now")
        self.assertEqual(selection["action"], "get_today_events")

    async def test_falls_back_to_json_and_then_to_none(self):
        from app.agent import selector

        async def fake_json(payload):
            return {"choices": [{"message": {"content": '{"action": "get_upcoming_events", "arguments": {"days": 3}}'}}]}

        selection = await selector.select_action(fake_json, [], "now")
        self.assertEqual(selection["action"], "get_upcoming_events")
        self.assertEqual(selection["arguments"], {"days": 3})

        async def fake_prose(payload):
            return {"choices": [{"message": {"content": "Sure, here is your schedule!"}}]}

        selection = await selector.select_action(fake_prose, [], "now")
        self.assertEqual(selection["action"], "none")

        async def fake_boom(payload):
            raise RuntimeError("network down")

        selection = await selector.select_action(fake_boom, [], "now")
        self.assertEqual(selection["action"], "none")

    async def test_extracts_json_from_mixed_prose(self):
        from app.agent import selector

        self.assertEqual(
            selector._parse_json_selection(
                'Sure! {"action": "send_email", "arguments": {"to": ["a@x.com"]}} Here you go.'
            ),
            {"action": "send_email", "arguments": {"to": ["a@x.com"]}},
        )
        self.assertEqual(selector._parse_json_selection("no braces at all")["action"], "none")
        self.assertEqual(
            selector._parse_json_selection('```json\n{"action": "none"}\n```')["action"], "none"
        )
        # Braces inside strings must not confuse the extractor.
        self.assertEqual(
            selector._parse_json_selection('Note {x} then {"action": "none"}')["action"], "none"
        )

    async def test_prompt_keeps_vague_intent_examples(self):
        from app.agent import selector

        # Regression guard: the live endpoint ignores `tools`, so the prompt
        # itself must teach vague-intent selection with few-shot examples.
        self.assertIn("I want to send an email.", selector.SELECTOR_SYSTEM)
        self.assertIn('"action": "none"', selector.SELECTOR_SYSTEM)


class ValidationTests(unittest.TestCase):
    def test_rejects_unknown_action_and_bad_ranges(self):
        from app.agent.action_models import AvailabilityArgs, CreateEventArgs, UpcomingArgs
        from app.agent.tool_registry import ToolRegistry
        from app.agent.tool_executor import execute_tool

        registry = ToolRegistry()
        result = asyncio.new_event_loop().run_until_complete(
            execute_tool(registry, "delete_everything", {}, {})
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "unsupported_action")

        with self.assertRaises(Exception):
            UpcomingArgs.model_validate({"days": 500})
        with self.assertRaises(Exception):
            AvailabilityArgs.model_validate(
                {"start": "2026-11-02T10:00:00+00:00", "end": "2026-11-02T09:00:00+00:00"}
            )
        with self.assertRaises(Exception):
            CreateEventArgs.model_validate({"summary": "x"})


class ConfirmationTests(unittest.TestCase):
    def test_explicit_confirm_word_required_and_expiry(self):
        from app.agent.confirmation import (
            ConfirmationStore,
            is_explicit_confirmation,
        )

        self.assertTrue(is_explicit_confirmation("Confirm."))
        self.assertTrue(is_explicit_confirmation("Yes, confirm that meeting"))
        self.assertFalse(is_explicit_confirmation("yes"))
        self.assertFalse(is_explicit_confirmation("yeah sounds good"))

        store = ConfirmationStore()
        store.propose("s1", "create_calendar_event", {"summary": "X"}, "proposal")
        self.assertIsNotNone(store.get_valid("s1"))
        consumed = store.consume("s1")
        self.assertIsNotNone(consumed)
        self.assertIsNone(store.get_valid("s1"))  # single-use

        store.propose("s2", "create_calendar_event", {"summary": "X"}, "proposal")
        pending = store._pending["s2"]
        pending.created_at = pending.created_at - timedelta(minutes=10)
        self.assertIsNone(store.get_valid("s2"))  # expired

    def test_matching_details_requires_exact_event(self):
        from app.agent.confirmation import matching_details

        stored = {"summary": "Sync", "start": "A", "end": "B"}
        self.assertTrue(matching_details(stored, dict(stored)))
        self.assertFalse(matching_details(stored, {**stored, "start": "C"}))


class OAuthTests(unittest.TestCase):
    def test_state_single_use_and_authorize_url(self):
        from app.integrations.google import oauth

        state = oauth.create_state()
        self.assertTrue(oauth.consume_state(state))
        self.assertFalse(oauth.consume_state(state))  # no reuse
        self.assertFalse(oauth.consume_state(None))
        self.assertFalse(oauth.consume_state("bogus"))

        url = oauth.build_authorize_url("cid", "http://127.0.0.1:8002/auth/google/callback", "s")
        self.assertIn("accounts.google.com", url)
        self.assertIn("access_type=offline", url)
        self.assertIn("calendar", url)
        scope_part = url.split("scope=")[1].split("&")[0].lower()
        self.assertIn("gmail", scope_part)
        self.assertIn("calendar", scope_part)

    def test_login_requires_configuration(self):
        blank_google = {"GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": "", "GOOGLE_REDIRECT_URI": ""}
        with patch.dict("os.environ", blank_google, clear=False):
            with patch.object(main, "_app_settings", None):
                with TestClient(main.app) as client:
                    response = client.get("/auth/google/login", follow_redirects=False)
        self.assertEqual(response.status_code, 503)

    def test_callback_rejects_bad_state(self):
        with TestClient(main.app) as client:
            response = client.get("/auth/google/callback?code=x&state=bogus")
        self.assertEqual(response.status_code, 400)

    def test_not_connected_gate_prompts_login(self):
        import os
        from app.integrations.google.tools import GoogleDeps

        async def fake_select(post_chat, messages, label):
            return {"action": "get_today_events", "arguments": {}}

        deps = GoogleDeps(
            client_id="test-id",
            client_secret="test-secret",
            redirect_uri="http://127.0.0.1:8002/auth/google/callback",
        )
        with patch.dict(os.environ, {"MODEL_API_KEY": "test-key"}):
            with patch.object(main, "_google_deps", return_value=deps):
                with patch("app.integrations.google.token_store.is_connected", return_value=False):
                    with patch("app.agent.selector.select_action", fake_select):
                        with TestClient(main.app) as client:
                            response = client.post(
                                "/api/chat/stream",
                                json={"messages": [{"role": "user", "content": "What meetings today?"}]},
                            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("not connected", response.text.lower())
        self.assertIn("/auth/google/login", response.text)
        events = []
        for frame in response.text.split("\n\n"):
            for line in frame.splitlines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
        actions = [e for e in events if e.get("type") == "action"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].get("url"), "http://127.0.0.1:8002/auth/google/login")
        spoken = " ".join(e.get("text", "") for e in events if e.get("type") == "delta")
        self.assertIn("not connected", spoken.lower())
        self.assertNotIn("http", spoken)

    def test_callback_access_denied_explains_test_users(self):
        with TestClient(main.app) as client:
            response = client.get("/auth/google/callback?error=access_denied&state=x")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Test users", response.text)

    def test_status_and_disconnect(self):
        with TestClient(main.app) as client:
            status = client.get("/api/google/status")
            self.assertIn(status.status_code, (200,))
            self.assertIn("connected", status.json())
            response = client.post("/api/google/disconnect")
            self.assertEqual(response.json(), {"connected": False})


class MeetingDraftFlowTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(main, "_google_connect_message", return_value=None)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _post(self, client, text, session):
        return client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": text}], "session_id": session},
        )

    def _texts(self, response):
        events = []
        for frame in response.text.split("\n\n"):
            for line in frame.splitlines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
        return " ".join(event.get("text", "") for event in events)

    def _run_client(self, selections):
        import os
        from app.agent.tool_executor import ToolResult

        async def fake_select(post_chat, messages, label):
            return selections[min(fake_select.calls, len(selections) - 1)]

        fake_select.calls = 0
        real_select = fake_select

        async def counting_select(post_chat, messages, label):
            result = await real_select(post_chat, messages, label)
            counting_select.calls += 1
            return result

        counting_select.calls = 0

        async def fake_execute(registry, action, args, context):
            if not context.get("confirmed"):
                proposal = (
                    f"Meeting: {args.get('summary')} from {args.get('start')} "
                    f"to {args.get('end')}. Reply Confirm."
                )
                return ToolResult(ok=False, code="needs_confirmation",
                                  message=proposal, needs_confirmation=True,
                                  proposal=proposal, arguments=args)
            return ToolResult(ok=True, code="ok", message="created", data={"id": "e1"})

        return (
            patch.dict(os.environ, {"MODEL_API_KEY": "test-key"}),
            counting_select,
            fake_execute,
        )

    def test_guided_flow_summary_start_end(self):
        env, fake_select, fake_execute = self._run_client([
            {"action": "create_calendar_event", "arguments": {}},
        ])
        expected_day = (datetime.now().astimezone() + timedelta(days=1)).date().isoformat()
        with env:
            with patch("app.agent.selector.select_action", fake_select):
                with patch("app.agent.tool_executor.execute_tool", fake_execute):
                    with TestClient(main.app) as client:
                        first = self._post(client, "schedule a meeting", "meet-1")
                        self.assertIn("What should I call the meeting", self._texts(first))
                        second = self._post(client, "standup", "meet-1")
                        self.assertIn("When does it start", self._texts(second))
                        third = self._post(client, "tomorrow at 10am", "meet-1")
                        self.assertIn("When does it end", self._texts(third))
                        fourth = self._post(client, "11", "meet-1")
                        proposal = self._texts(fourth)
                        self.assertIn("standup", proposal)
                        self.assertIn(expected_day, proposal)
                        self.assertIn("10:00", proposal)

    def test_one_go_meeting_then_title(self):
        env, fake_select, fake_execute = self._run_client([
            {"action": "create_calendar_event", "arguments": {}},
        ])
        expected_day = (datetime.now().astimezone() + timedelta(days=1)).date().isoformat()
        with env:
            with patch("app.agent.selector.select_action", fake_select):
                with patch("app.agent.tool_executor.execute_tool", fake_execute):
                    with TestClient(main.app) as client:
                        first = self._post(client, "schedule sync tomorrow at 10 to 11", "meet-2")
                        self.assertIn("What should I call the meeting", self._texts(first))
                        second = self._post(client, "Sync", "meet-2")
                        proposal = self._texts(second)
                        self.assertIn("Sync", proposal)
                        self.assertIn(expected_day, proposal)
                        self.assertIn("11:00", proposal)


class TokenStoreTests(unittest.TestCase):
    def test_roundtrip_and_clear_in_tmp(self):
        from app.integrations.google import token_store

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "tokens.json"
            self.assertIsNone(token_store.load_tokens(path))
            token_store.save_tokens({"refresh_token": "r", "scope": "s"}, path)
            loaded = token_store.load_tokens(path)
            self.assertEqual(loaded["refresh_token"], "r")
            self.assertNotIn("access_token", loaded)
            token_store.clear_tokens(path)
            self.assertIsNone(token_store.load_tokens(path))


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

    async def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        return self._handler("POST", url, kwargs)


class CalendarServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_and_availability(self):
        from app.integrations.google import calendar as service

        async def ensure_token(force_refresh=False):
            return "tok"

        def handler(method, url, kwargs):
            return FakeHttpResponse(200, {"items": [
                {"id": "e1", "summary": "Standup",
                 "start": {"dateTime": "2026-11-02T10:00:00+05:30"},
                 "end": {"dateTime": "2026-11-02T10:30:00+05:30"}},
            ]})

        upcoming = await service.get_upcoming_events(
            FakeHttpClient(handler), ensure_token, _utc(2026, 11, 1), 7, 10
        )
        self.assertEqual(upcoming["count"], 1)
        self.assertEqual(upcoming["events"][0]["summary"], "Standup")

        free = await service.check_availability(
            FakeHttpClient(lambda m, u, k: FakeHttpResponse(200, {"items": []})),
            ensure_token, _utc(2026, 11, 3, 10), _utc(2026, 11, 3, 11),
        )
        self.assertTrue(free["free"])

        busy = await service.check_availability(
            FakeHttpClient(handler), ensure_token, _utc(2026, 11, 2, 9), _utc(2026, 11, 2, 11)
        )
        self.assertFalse(busy["free"])
        self.assertEqual(len(busy["conflicts"]), 1)

    async def test_401_refreshes_once_then_succeeds(self):
        from app.integrations.google import calendar as service

        calls = {"n": 0, "tokens": 0}

        async def ensure_token(force_refresh=False):
            calls["tokens"] += 1
            return "tok-new" if force_refresh else "tok-old"

        def handler(method, url, kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeHttpResponse(401, {})
            return FakeHttpResponse(200, {"items": []})

        result = await service.get_upcoming_events(
            FakeHttpClient(handler), ensure_token, _utc(2026, 11, 1), 7, 10
        )
        self.assertEqual(result["count"], 0)
        self.assertEqual(calls["tokens"], 2)

    async def test_rate_limit_maps_to_structured_error(self):
        from app.integrations.google import calendar as service

        async def ensure_token(force_refresh=False):
            return "tok"

        def handler(method, url, kwargs):
            return FakeHttpResponse(429, {})

        with self.assertRaises(Exception) as ctx:
            await service.get_upcoming_events(
                FakeHttpClient(handler), ensure_token, _utc(2026, 11, 1), 7, 10
            )
        self.assertEqual(ctx.exception.code, "rate_limited")


class ChatGateTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(main, "_google_connect_message", return_value=None)
        self.addCleanup(patcher.stop)
        patcher.start()
    def _stream_text(self, response):
        events = []
        for frame in response.text.split("\n\n"):
            for line in frame.splitlines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
        return events

    def test_ordinary_chat_unchanged_no_session(self):
        async def fake_stream(messages, _api_key):
            yield main._sse_event({"type": "delta", "text": "Hello"})
            yield main._sse_event({"type": "done"})

        import os

        with patch.dict(os.environ, {"MODEL_API_KEY": "test-key"}):
            with patch.object(main, "_stream_muse_chat", fake_stream):
                with TestClient(main.app) as client:
                    response = client.post(
                        "/api/chat/stream",
                        json={"messages": [{"role": "user", "content": "Tell me a joke"}]},
                    )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [e["type"] for e in self._stream_text(response)], ["delta", "done"]
        )

    def test_today_meetings_skips_selector(self):
        import os

        async def exploding_select(post_chat, messages, label):
            raise AssertionError("selector should be skipped on the fast path")

        async def fake_verbalize(messages, api_key, action, data):
            return [main._sse_event({"type": "delta", "text": "You have 1 meeting."}),
                    main._sse_event({"type": "done"})]

        async def fake_execute(registry, action, args, context):
            from app.agent.tool_executor import ToolResult

            self.assertEqual(action, "get_today_events")
            return ToolResult(ok=True, code="ok", message="ok",
                              data={"count": 1, "events": [{"summary": "Standup"}]})

        with patch.dict(os.environ, {"MODEL_API_KEY": "test-key"}):
            with patch("app.agent.selector.select_action", exploding_select):
                with patch.object(main, "_verbalize_tool_result", fake_verbalize):
                    with patch("app.agent.tool_executor.execute_tool", fake_execute):
                        with TestClient(main.app) as client:
                            response = client.post(
                                "/api/chat/stream",
                                json={"messages": [{"role": "user", "content": "What meetings do I have today?"}]},
                            )
        events = self._stream_text(response)
        texts = [e.get("text", "") for e in events]
        self.assertIn("You have 1 meeting.", texts)
        dones = [e for e in events if e.get("type") == "done" and e.get("server_ms") is not None]
        self.assertTrue(dones)
        self.assertEqual(dones[0]["server_ms"].get("selector_ms"), 0)

    def test_calendar_read_flow_end_to_end_mocked(self):
        import os

        async def fake_select(post_chat, messages, label):
            self.assertEqual(len(main._agent_registry().openai_tools()), 12)
            return {"action": "get_today_events", "arguments": {}}

        async def fake_verbalize(messages, api_key, action, data):
            self.assertEqual(action, "get_today_events")
            self.assertEqual(data["count"], 1)
            return [main._sse_event({"type": "delta", "text": "You have 1 meeting."}),
                    main._sse_event({"type": "done"})]

        async def fake_execute(registry, action, args, context):
            from app.agent.tool_executor import ToolResult

            self.assertEqual(action, "get_today_events")
            return ToolResult(ok=True, code="ok", message="ok",
                              data={"count": 1, "events": [{"summary": "Standup"}]})

        with patch.dict(os.environ, {"MODEL_API_KEY": "test-key"}):
            with patch("app.agent.selector.select_action", fake_select):
                with patch.object(main, "_verbalize_tool_result", fake_verbalize):
                    with patch("app.agent.tool_executor.execute_tool", fake_execute):
                        with TestClient(main.app) as client:
                            response = client.post(
                                "/api/chat/stream",
                                json={
                                    "messages": [{"role": "user", "content": "What meetings today?"}],
                                    "session_id": "sess-1",
                                },
                            )
        texts = [e.get("text", "") for e in self._stream_text(response)]
        self.assertIn("You have 1 meeting.", texts)

    def test_create_requires_confirmation_then_confirms(self):
        import os

        async def fake_chat(messages, _api_key):
            yield main._sse_event({"type": "delta", "text": "Just chatting."})
            yield main._sse_event({"type": "done"})

        async def fake_select(post_chat, messages, label):
            return {"action": "create_calendar_event",
                    "arguments": {"summary": "Sync",
                                  "start": "2026-11-02T10:00:00+05:30",
                                  "end": "2026-11-02T10:30:00+05:30"}}

        async def fake_execute(registry, action, args, context):
            from app.agent.tool_executor import ToolResult

            if not context.get("confirmed"):
                return ToolResult(ok=False, code="needs_confirmation",
                                  message="Please confirm.",
                                  needs_confirmation=True, proposal="Please confirm: 'Sync'.",
                                  arguments=args)
            return ToolResult(ok=True, code="ok", message="created",
                              data={"id": "e1", "summary": "Sync"})

        with patch.dict(os.environ, {"MODEL_API_KEY": "test-key"}):
            with patch("app.agent.selector.select_action", fake_select):
                with patch("app.agent.tool_executor.execute_tool", fake_execute):
                    with patch.object(main, "_stream_muse_chat", fake_chat):
                        with TestClient(main.app) as client:
                            first = client.post(
                                "/api/chat/stream",
                                json={"messages": [{"role": "user",
                                                    "content": "Schedule a meeting Sync tomorrow at 10 AM"}],
                                      "session_id": "sess-2"},
                            )
                            self.assertIn("confirm", first.text.lower())
                            second = client.post(
                                "/api/chat/stream",
                                json={"messages": [{"role": "user", "content": "Confirm."}],
                                      "session_id": "sess-2"},
                            )
                            self.assertIn("scheduled successfully", second.text)
                            # single-use: confirming again must not recreate
                            third = client.post(
                                "/api/chat/stream",
                                json={"messages": [{"role": "user", "content": "Confirm."}],
                                      "session_id": "sess-2"},
                            )
                            self.assertNotIn("scheduled successfully", third.text)


if __name__ == "__main__":
    unittest.main()
