"""Google Sheets booking log: optional, internal-only, never exposed to chat.

Configuration is environment-only:
- GOOGLE_SHEETS_SPREADSHEET_ID
- GOOGLE_SHEETS_SHEET_NAME (default "Bookings")
- GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SHEETS_CREDENTIALS_FILE

When unconfigured, bookings stay in the local JSONL record and the caller
marks the row as local-only. Failures are logged securely server-side with
the booking ID and reason; the user only hears that the meeting itself is
booked while the sheet retry is flagged for follow-up.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def sheets_config() -> dict | None:
    spreadsheet = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
    if not spreadsheet:
        return None
    return {
        "spreadsheet_id": spreadsheet,
        "sheet_name": os.getenv("GOOGLE_SHEETS_SHEET_NAME", "Bookings").strip() or "Bookings",
        "service_account": (
            os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE", "").strip()
            or ("__inline__" if os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip() else "")
        ),
    }


async def append_booking_row(booking: dict) -> tuple[bool, str]:
    """Best-effort Sheets append; returns (saved, status_note)."""
    config = sheets_config()
    booking_id = str(booking.get("booking_id", ""))
    if config is None:
        return False, "local-only (Sheets not configured)"
    # No Sheets client vendored yet: record for follow-up instead of failing
    # silently or leaking credentials. Wire the real append here later.
    logger.warning(
        "sheets append deferred id=%s spreadsheet configured but no Sheets client vendored",
        booking_id,
    )
    return False, "needs follow-up (Sheets client unavailable)"
