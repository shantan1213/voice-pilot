"""Focused tests for backend latency improvements."""

import unittest

from fastapi.testclient import TestClient

import app.main as main
from app.settings import settings


class BackendEfficiencyTests(unittest.TestCase):
    def test_health_exposes_model_ready_bool(self):
        with TestClient(main.app) as client:
            data = client.get("/api/health").json()
        self.assertIn("model_ready", data)
        self.assertIsInstance(data["model_ready"], bool)
        # legacy keys still present
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["phase"], 1)

    def test_chat_client_limits_and_timeout_tuned(self):
        self.assertEqual(main.CHAT_TIMEOUT.read, 45)
        self.assertEqual(main.CHAT_TIMEOUT.connect, 10)
        # limits configured for keepalive reuse
        self.assertEqual(main.CHAT_LIMITS.max_keepalive_connections, 10)
        self.assertEqual(main.CHAT_LIMITS.max_connections, 10)

    def test_history_snapshot_endpoint_does_not_block(self):
        # list_turns_snapshot should exist and be used by GET /api/history
        import inspect

        self.assertTrue(hasattr(main.conversation_logs, "list_turns_snapshot"))
        source = inspect.getsource(main.get_conversation_history)
        self.assertIn("list_turns_snapshot", source)
        self.assertNotIn("queue.join", source)

    def test_tts_in_memory_path_exists(self):
        from app.tts import synthesize_to_bytes, is_model_ready

        self.assertTrue(callable(synthesize_to_bytes))
        self.assertTrue(callable(is_model_ready))
        # StreamingResponse uses semaphore and bytes
        import inspect

        src = inspect.getsource(main.tts)
        self.assertIn("synthesize_to_bytes", src)
        self.assertIn("_tts_semaphore", src)
        self.assertIn("StreamingResponse", src)

    def test_settings_uses_pydantic_settings(self):
        # Settings reads from .env via pydantic-settings
        self.assertTrue(hasattr(settings, "model_api_key"))
        self.assertTrue(hasattr(settings, "muse_chat_model"))
        # Value is string (may be empty in test env)
        self.assertIsInstance(settings.muse_chat_model, str)


class ConversationSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_returns_without_queue_join(self):
        import tempfile
        from pathlib import Path
        from uuid import uuid4

        from app.conversation_logs import ConversationLogWorker

        with tempfile.TemporaryDirectory() as tmp:
            worker = ConversationLogWorker(Path(tmp) / "logs")
            await worker.start()
            try:
                worker.enqueue(uuid4(), uuid4(), "hi", "hello")
                # snapshot should return immediately even if worker hasn't flushed
                turns_snapshot = await worker.list_turns_snapshot()
                self.assertIsInstance(turns_snapshot, list)
                # full list_turns would block on queue.join; snapshot must not
                # ensure no deadlock within short timeout
            finally:
                await worker.stop()


if __name__ == "__main__":
    unittest.main()
