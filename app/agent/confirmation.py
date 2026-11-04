"""Backend store for pending event-creation confirmations.

A proposal is bound to one session, holds the exact validated event, expires
after `CONFIRMATION_TTL`, and is single-use: it is consumed before the Google
API is called, so a retry or duplicate message cannot create two events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.agent.action_models import CONFIRMATION_TTL

#: An unrelated "yes" must never confirm. Only the explicit word counts.
_CONFIRM_RE = re.compile(r"\bconfirm\b", re.IGNORECASE)


def is_explicit_confirmation(text: str) -> bool:
    """Return True only when the message explicitly says 'confirm'."""
    return bool(_CONFIRM_RE.search(text or ""))


_AFFIRM_RE = re.compile(
    r"\b(confirm|confirmed|yes|yeah|yep|yup|sure|ok|okay|do it|send it|go ahead|schedule it)\b",
    re.IGNORECASE,
)
_DATE_WORD_RE = re.compile(
    r"\b(today|tomorrow|morning|afternoon|evening|noon|midnight|next|week"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|january|february|march|april|june|july|august|september|october|november|december)\b",
    re.IGNORECASE,
)


def is_affirmative_confirmation(text: str) -> bool:
    """Short explicit agreement with no new details counts as confirmation.

    Anything carrying an address, a number, a date word, or a long message
    is treated as new input, never as confirmation.
    """
    lowered = (text or "").strip().lower()
    if not lowered or "@" in lowered:
        return False
    if re.search(r"\d", lowered):
        return False
    if _DATE_WORD_RE.search(lowered):
        return False
    if len(lowered.split()) > 8:
        return False
    return bool(_AFFIRM_RE.search(lowered))


@dataclass
class PendingAction:
    session_id: str
    action: str
    arguments: dict
    proposal: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    consumed: bool = False

    def expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return (now - self.created_at) > CONFIRMATION_TTL


class ConfirmationStore:
    """In-memory pending proposals, keyed by session."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingAction] = {}

    def propose(
        self,
        session_id: str,
        action: str,
        arguments: dict,
        proposal: str,
    ) -> PendingAction:
        pending = PendingAction(
            session_id=session_id,
            action=action,
            arguments=dict(arguments),
            proposal=proposal,
        )
        self._pending[session_id] = pending
        return pending

    def get_valid(self, session_id: str) -> PendingAction | None:
        pending = self._pending.get(session_id)
        if pending is None or pending.consumed or pending.expired():
            self._pending.pop(session_id, None)
            return None
        return pending

    def consume(self, session_id: str) -> PendingAction | None:
        """Return the valid pending action and mark it single-use."""
        pending = self.get_valid(session_id)
        if pending is None:
            return None
        pending.consumed = True
        self._pending.pop(session_id, None)
        return pending

    def discard(self, session_id: str) -> None:
        self._pending.pop(session_id, None)


#: Exact-match keys per confirmable action. A confirmation applies only when
#: every listed field matches the proposal.
MATCH_KEYS: dict[str, tuple[str, ...]] = {
    "create_calendar_event": ("summary", "start", "end"),
    "send_email": ("to", "subject", "body"),
    "create_task": ("title", "due", "notes"),
    "update_task": ("task_id", "task_list_id", "title", "notes", "due", "status"),
    "complete_task": ("task_id", "task_list_id"),
}


def _normalized(action: str, key: str, value: object) -> str:
    if action == "send_email" and key == "to":
        # Recipient order must not matter; accept lists or strings.
        raw_parts: list[str] = []
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            raw_parts.extend(str(item or "").replace(";", ",").split(","))
        parts = [part.strip().lower() for part in raw_parts]
        return ",".join(sorted(part for part in parts if part))
    return str(value or "").strip()


def matching_details_for(action: str, stored: dict, incoming: dict) -> bool:
    """Check the incoming confirmation targets the exact proposed operation."""
    keys = MATCH_KEYS.get(action)
    if not keys:
        return False
    for key in keys:
        if _normalized(action, key, stored.get(key)) != _normalized(action, key, incoming.get(key)):
            return False
    return True


def matching_details(stored: dict, incoming: dict) -> bool:
    """Check the incoming confirmation targets the exact proposed event."""
    for key in ("summary", "start", "end"):
        if str(stored.get(key, "")).strip() != str(incoming.get(key, "")).strip():
            return False
    return True
