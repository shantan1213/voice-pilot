"""Non-blocking, idempotent plain-text conversation logging."""

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

logger = logging.getLogger(__name__)

_TURN_MARKER = re.compile(r"^TURN ([0-9a-f]{32})$")


@dataclass(frozen=True)
class ConversationTurn:
    session_id: UUID
    turn_id: UUID
    user: str
    assistant: str
    timestamp: str


@dataclass
class _ClearRequest:
    complete: asyncio.Future[None]


class ConversationLogWorker:
    """Queue complete turns and write them away from FastAPI's event loop."""

    def __init__(self, directory: Path, queue_size: int = 256) -> None:
        self.directory = directory
        self._queue_size = queue_size
        self._queue: asyncio.Queue[ConversationTurn | _ClearRequest | None] = asyncio.Queue(
            maxsize=queue_size
        )
        self._worker: asyncio.Task[None] | None = None
        self._logged_ids: set[tuple[str, str]] = set()
        self._queued_ids: set[tuple[str, str]] = set()

    async def start(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._queue = asyncio.Queue(maxsize=self._queue_size)
        self._queued_ids.clear()
        self._logged_ids = await asyncio.to_thread(self._load_logged_ids)
        self._worker = asyncio.create_task(self._run(), name="conversation-log-writer")

    async def stop(self) -> None:
        worker = self._worker
        if worker is None:
            return
        await self._queue.put(None)
        await worker
        self._worker = None

    def enqueue(
        self, session_id: UUID, turn_id: UUID, user: str, assistant: str
    ) -> Literal["queued", "duplicate", "full"]:
        """Queue a complete turn without waiting for disk I/O."""
        key = (session_id.hex, turn_id.hex)
        if key in self._logged_ids or key in self._queued_ids:
            return "duplicate"
        turn = ConversationTurn(
            session_id=session_id,
            turn_id=turn_id,
            user=user,
            assistant=assistant,
            timestamp=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        )
        try:
            self._queue.put_nowait(turn)
        except asyncio.QueueFull:
            logger.warning("Conversation log queue is full; a completed turn was not queued.")
            return "full"
        self._queued_ids.add(key)
        return "queued"

    async def list_turns(self) -> list[dict[str, str]]:
        await self._queue.join()
        return await asyncio.to_thread(self._read_turns)

    async def list_turns_snapshot(self) -> list[dict[str, str]]:
        return await asyncio.to_thread(self._read_turns)

    async def clear(self) -> None:
        if self._worker is None or self._worker.done():
            raise RuntimeError("Conversation logging is not running.")
        complete = asyncio.get_running_loop().create_future()
        await self._queue.put(_ClearRequest(complete))
        await complete

    async def wait_until_idle(self) -> None:
        """Wait for pending writes; primarily useful for orderly shutdown and tests."""
        await self._queue.join()

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                if isinstance(item, _ClearRequest):
                    try:
                        await asyncio.to_thread(self._clear_files)
                    except OSError as exc:
                        logger.warning("Could not clear conversation logs (%s).", type(exc).__name__)
                        if not item.complete.done():
                            item.complete.set_exception(exc)
                    else:
                        self._logged_ids.clear()
                        if not item.complete.done():
                            item.complete.set_result(None)
                    continue

                key = (item.session_id.hex, item.turn_id.hex)
                try:
                    await asyncio.to_thread(self._append_turn, item)
                except OSError as exc:
                    self._queued_ids.discard(key)
                    logger.warning("Could not write a conversation log (%s).", type(exc).__name__)
                else:
                    self._queued_ids.discard(key)
                    self._logged_ids.add(key)
            finally:
                self._queue.task_done()

    def _load_logged_ids(self) -> set[tuple[str, str]]:
        logged: set[tuple[str, str]] = set()
        try:
            paths = tuple(self.directory.glob("session-*.txt"))
        except OSError as exc:
            logger.warning("Could not inspect conversation logs (%s).", type(exc).__name__)
            return logged
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            session_hex = path.stem.removeprefix("session-")
            try:
                session_hex = UUID(hex=session_hex).hex
                for line in path.read_text(encoding="utf-8").splitlines():
                    match = _TURN_MARKER.fullmatch(line)
                    if match:
                        logged.add((session_hex, match.group(1)))
            except (OSError, ValueError, UnicodeError) as exc:
                logger.warning("Could not read conversation log (%s).", type(exc).__name__)
        return logged

    def _append_turn(self, turn: ConversationTurn) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"session-{turn.session_id.hex}.txt"
        with path.open("a", encoding="utf-8", newline="\n") as log_file:
            if path.stat().st_size == 0:
                log_file.write(
                    "Voice-Pilot conversation log\n"
                    f"Session ID: {turn.session_id}\n\n"
                )
            log_file.write(
                f"TURN {turn.turn_id.hex}\n"
                f"Timestamp: {turn.timestamp}\n"
                f"User: {json.dumps(turn.user, ensure_ascii=False)}\n"
                f"Assistant: {json.dumps(turn.assistant, ensure_ascii=False)}\n\n"
            )

    def _read_turns(self) -> list[dict[str, str]]:
        turns: list[dict[str, str]] = []
        try:
            paths = tuple(self.directory.glob("session-*.txt"))
        except OSError as exc:
            logger.warning("Could not list conversation logs (%s).", type(exc).__name__)
            return turns
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            session_hex = path.stem.removeprefix("session-")
            try:
                session_id = str(UUID(hex=session_hex))
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, ValueError, UnicodeError) as exc:
                logger.warning("Could not read conversation log (%s).", type(exc).__name__)
                continue
            for index, line in enumerate(lines):
                match = _TURN_MARKER.fullmatch(line)
                if not match:
                    continue
                try:
                    timestamp = lines[index + 1].removeprefix("Timestamp: ")
                    user = json.loads(lines[index + 2].removeprefix("User: "))
                    assistant = json.loads(lines[index + 3].removeprefix("Assistant: "))
                except (IndexError, json.JSONDecodeError):
                    logger.warning("Skipping a malformed conversation log turn.")
                    continue
                if isinstance(user, str) and isinstance(assistant, str):
                    turns.append(
                        {
                            "session_id": session_id,
                            "turn_id": str(UUID(hex=match.group(1))),
                            "timestamp": timestamp,
                            "user": user,
                            "assistant": assistant,
                        }
                    )
        turns.sort(key=lambda turn: (turn["timestamp"], turn["session_id"]))
        return turns

    def _clear_files(self) -> None:
        if not self.directory.exists():
            return
        for path in self.directory.glob("session-*.txt"):
            if path.is_symlink() or not path.is_file():
                continue
            path.unlink()
        if not any(self.directory.iterdir()):
            shutil.rmtree(self.directory)
