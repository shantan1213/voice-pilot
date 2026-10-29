import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from app.conversation_logs import ConversationLogWorker


class ConversationLogWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.log_directory = Path(self.temporary_directory.name) / "conversation_logs"
        self.worker = ConversationLogWorker(self.log_directory)
        await self.worker.start()

    async def asyncTearDown(self):
        await self.worker.stop()
        self.temporary_directory.cleanup()

    async def test_writes_ordered_readable_turn_once_and_recovers_deduplication(self):
        session_id = uuid4()
        first_id = uuid4()
        second_id = uuid4()

        self.assertEqual(
            self.worker.enqueue(session_id, first_id, "Hello\nthere", "Hi, welcome."),
            "queued",
        )
        self.assertEqual(
            self.worker.enqueue(session_id, first_id, "Hello\nthere", "Hi, welcome."),
            "duplicate",
        )
        self.assertEqual(self.worker.enqueue(session_id, second_id, "How are you?", "Doing well."), "queued")

        turns = await self.worker.list_turns()
        self.assertEqual([turn["turn_id"] for turn in turns], [str(first_id), str(second_id)])
        self.assertEqual(turns[0]["user"], "Hello\nthere")
        self.assertEqual(turns[0]["assistant"], "Hi, welcome.")
        files = list(self.log_directory.glob("session-*.txt"))
        self.assertEqual(len(files), 1)
        contents = files[0].read_text(encoding="utf-8")
        self.assertIn(f"Session ID: {session_id}", contents)
        self.assertIn(f"TURN {first_id.hex}", contents)
        self.assertEqual(contents.count(f"TURN {first_id.hex}"), 1)

        restarted = ConversationLogWorker(self.log_directory)
        await restarted.start()
        try:
            self.assertEqual(
                restarted.enqueue(session_id, first_id, "Hello\nthere", "Hi, welcome."),
                "duplicate",
            )
        finally:
            await restarted.stop()

    async def test_clear_waits_for_prior_writes_and_allows_future_turns(self):
        session_id = uuid4()
        first_id = uuid4()
        self.assertEqual(self.worker.enqueue(session_id, first_id, "First", "Reply"), "queued")

        await self.worker.clear()

        self.assertEqual(await self.worker.list_turns(), [])
        self.assertFalse(self.log_directory.exists())
        self.assertEqual(self.worker.enqueue(session_id, first_id, "First", "Reply"), "queued")
        self.assertEqual(len(await self.worker.list_turns()), 1)

    async def test_file_write_error_does_not_stop_the_worker(self):
        self.log_directory.write_text("not a directory", encoding="utf-8")
        self.assertEqual(self.worker.enqueue(uuid4(), uuid4(), "Question", "Answer"), "queued")

        await self.worker.wait_until_idle()

        self.assertTrue(self.worker._worker is not None and not self.worker._worker.done())
        self.assertEqual(self.worker.enqueue(uuid4(), uuid4(), "Another", "Still running"), "queued")


if __name__ == "__main__":
    unittest.main()
