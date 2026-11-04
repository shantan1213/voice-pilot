"""Local timezone helper (user-entered meeting times are local, never UTC)."""

from __future__ import annotations

from datetime import datetime, tzinfo


def local_tz() -> tzinfo:
    """Return the server's local timezone (the user's Windows timezone)."""
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def today_label(now: datetime | None = None) -> str:
    """Human-readable local date label for action-selection prompts."""
    now = now or datetime.now().astimezone()
    return now.strftime("%A, %Y-%m-%d %H:%M %Z")
