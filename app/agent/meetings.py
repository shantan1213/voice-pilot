"""Meeting-only scheduling core: 10 hourly slots, validation, booking records.

The voice agent is a meeting scheduler. This module holds the pure logic so
both the chat flow and the REST booking endpoints share one implementation:

- 10 hourly slots per day starting at 9:00 AM (last slot 6:00 PM-7:00 PM).
- Availability speech that always names the exact date and timezone.
- Sanitized booking validation with a unique booking ID per appointment.
- Internal booking log (JSON lines, no secrets) plus an optional Google
  Sheets hook; Sheets failures are recorded for follow-up, never shown.
- Parallel HTML confirmation mail content (sent in the background).

No credentials, tokens, or service-account material live here.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

#: Fallback recipient used only when a voice caller gives no email during
#: testing. Never overwrites an email the user already supplied.
TESTING_EMAIL = "pixelssunny@gmail.com"

#: The meeting form always defaults to IST.
MEETING_TIMEZONE = "Asia/Kolkata"

#: Spoken names for terse tz abbreviations TTS would mangle.
_TZ_FRIENDLY = {"IST": "India Standard Time"}


def friendly_tz_label(tz_label: str) -> str:
    """Map a terse zone abbreviation to a speakable name when known."""
    return _TZ_FRIENDLY.get((tz_label or "").strip(), tz_label or "local time")


#: 10 hourly meeting slots: (start_hour, end_hour) in local time.
MEETING_SLOTS: tuple[tuple[int, int], ...] = (
    (9, 10),
    (10, 11),
    (11, 12),
    (12, 13),
    (13, 14),
    (14, 15),
    (15, 16),
    (16, 17),
    (17, 18),
    (18, 19),
)

MEETING_TYPES = ("video call", "phone call", "in-person")

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_PHONE_RE = re.compile(r"^[+\d][\d\s().-]{5,30}$")


def day_slot_bounds(day: datetime) -> list[tuple[datetime, datetime]]:
    """Local-aware (start, end) bounds for the 10 daily slots."""
    base = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return [
        (base.replace(hour=start), base.replace(hour=end))
        for start, end in MEETING_SLOTS
    ]


def slot_label(start: datetime) -> str:
    """Short spoken label such as '10:00 AM'."""
    return start.strftime("%I:%M %p").lstrip("0")


def free_slots(
    day: datetime, busy: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """Return the daily slots that do not overlap any busy interval."""
    free: list[tuple[datetime, datetime]] = []
    for start, end in day_slot_bounds(day):
        overlaps = any(b_start < end and start < b_end for b_start, b_end in busy)
        if not overlaps:
            free.append((start, end))
    return free


def spoken_date(day: datetime) -> str:
    """TTS-friendly date with no digits to stumble over ("Friday, November 6")."""
    return f"{day.strftime('%A, %B')} {day.day}"


def spoken_time(start: datetime) -> str:
    """TTS-friendly time ("10 AM", "10:30 AM"); never "oh oh"."""
    hour = start.hour % 12 or 12
    suffix = "AM" if start.hour < 12 else "PM"
    if start.minute:
        return f"{hour}:{start.minute:02d} {suffix}"
    return f"{hour} {suffix}"


def availability_speech(
    day: datetime, free: list[tuple[datetime, datetime]], tz_label: str
) -> str:
    """Short spoken availability line; the full slot list stays on screen.

    Reading every time aloud sounds robotic through TTS ("nine oh oh ay em"),
    so speech carries only the count, exact date, and timezone. The clickable
    slot cards show each time visually.
    """
    date_text = spoken_date(day)
    if not free:
        return (
            f"Tomorrow, {date_text}, is fully booked in {tz_label}. "
            "I can check the next available date if you like."
        )
    count = len(free)
    times = "1 open time" if count == 1 else f"{count} open times"
    return (
        f"Tomorrow, {date_text}, I found {times} in {tz_label}. "
        "They are shown on your screen. Which time works for you?"
    )


def meeting_intro_brief() -> str:
    """Concise pre-booking facts: duration, topic, types, what is needed."""
    return (
        "Meetings run one hour, between 9 in the morning and 7 in the evening. "
        "Tell me the topic, and choose a video call, phone call, or in-person meeting. "
        "I only need your name, email, and a time to book it."
    )


def new_booking_id() -> str:
    """Unique public booking reference (short, unguessable)."""
    return uuid.uuid4().hex[:8].upper()


def sanitize_text(value: object, limit: int = 200) -> str:
    """Collapse whitespace and cap length; never keep control characters."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:limit]


def validate_booking(payload: dict) -> dict:
    """Validate and sanitize one meeting-form submission.

    Returns sanitized fields or raises ValueError with a polite message for
    the voice agent to speak.
    """
    name = sanitize_text(payload.get("name"), 120)
    email = sanitize_text(payload.get("email"), 320).lower()
    phone = sanitize_text(payload.get("phone"), 40)
    topic = sanitize_text(payload.get("topic"), 200)
    meeting_type = sanitize_text(payload.get("meeting_type"), 30).lower()
    notes = sanitize_text(payload.get("notes"), 1000)
    tz_name = sanitize_text(payload.get("timezone"), 80)
    start_raw = str(payload.get("start") or "").strip()
    end_raw = str(payload.get("end") or "").strip()

    if len(name) < 2:
        raise ValueError("Please share your full name so I can book the meeting.")
    if not _EMAIL_RE.match(email):
        raise ValueError("That email address does not look valid. Please check it.")
    if phone and not _PHONE_RE.match(phone):
        raise ValueError("That phone number does not look valid. You can leave it blank.")
    if meeting_type not in MEETING_TYPES:
        raise ValueError("Please choose a video call, phone call, or in-person meeting.")
    if len(topic) < 2:
        raise ValueError("Please share a short meeting topic.")
    try:
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ValueError("Please pick one of the available times.") from exc
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Please pick one of the available times.")
    if end <= start or (end - start) > timedelta(hours=2):
        raise ValueError("Meetings run one hour. Please pick one available slot.")
    if start.hour < 9 or end.hour > 19 or start.date() != end.date():
        raise ValueError("Meetings run 9:00 AM to 7:00 PM on one day. Please pick a listed time.")

    return {
        "name": name,
        "email": email,
        "phone": phone,
        "topic": topic,
        "meeting_type": meeting_type,
        "notes": notes,
        "timezone": tz_name,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }


def booking_record_path(base_dir: Path) -> Path:
    return base_dir / "bookings.jsonl"


def booked_intervals(
    base_dir: Path, day_start: datetime, day_end: datetime
) -> list[tuple[datetime, datetime]]:
    """Busy intervals from our own booking log (works without Google)."""
    busy: list[tuple[datetime, datetime]] = []
    for record in read_booking_records(base_dir, limit=500):
        if str(record.get("status", "")).startswith("cancelled"):
            continue
        try:
            start = datetime.fromisoformat(str(record.get("start", "")).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(record.get("end", "")).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if start.tzinfo is None or end.tzinfo is None:
            continue
        if start < day_end and day_start < end:
            busy.append((start, end))
    return busy


def purge_cancelled_records(base_dir: Path) -> int:
    """Drop cancelled rows from the internal log; returns rows removed."""
    path = booking_record_path(base_dir)
    if not path.exists():
        return 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    kept: list[str] = []
    removed = 0
    for line in lines:
        try:
            parsed = json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        if isinstance(parsed, dict) and str(parsed.get("status", "")).startswith("cancelled"):
            removed += 1
            continue
        kept.append(line)
    if removed:
        try:
            path.write_text("\n".join(kept) + "\n" if kept else "", encoding="utf-8")
        except OSError:
            return 0
    return removed


def cancel_booking_record(base_dir: Path, booking_id: str) -> dict | None:
    """Mark one internal booking cancelled (frees its slot); returns the row."""
    from datetime import datetime as _dt

    wanted = (booking_id or "").strip().upper()
    path = booking_record_path(base_dir)
    if not path.exists():
        return None
    updated: dict | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    kept: list[str] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        if isinstance(parsed, dict) and str(parsed.get("booking_id", "")).upper() == wanted:
            parsed["status"] = "cancelled"
            parsed["cancelled_at"] = _dt.now().astimezone().isoformat()
            updated = parsed
            kept.append(json.dumps(parsed, ensure_ascii=True))
        else:
            kept.append(line)
    if updated is None:
        return None
    try:
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError:
        return None
    return updated


def read_booking_records(base_dir: Path, limit: int = 200) -> list[dict]:
    """Newest-first internal booking rows for the dashboard (no secrets)."""
    path = booking_record_path(base_dir)
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    for line in lines:
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return list(reversed(rows))


def append_booking_record(base_dir: Path, record: dict) -> None:
    """Append one internal booking row (JSON line); never logs secrets."""
    path = booking_record_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = {
        "booking_id": record.get("booking_id", ""),
        "created_at": record.get("created_at", ""),
        "meeting_date": record.get("meeting_date", ""),
        "meeting_time": record.get("meeting_time", ""),
        "timezone": record.get("timezone", ""),
        "name": record.get("name", ""),
        "email": record.get("email", ""),
        "phone": record.get("phone", "")[:4] + "***" if record.get("phone") else "",
        "topic": record.get("topic", ""),
        "meeting_type": record.get("meeting_type", ""),
        "notes": record.get("notes", ""),
        "status": record.get("status", ""),
        "sheets": record.get("sheets", ""),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=True) + "\n")
    logger.info(
        "meeting booked id=%s date=%s time=%s status=%s sheets=%s",
        safe["booking_id"],
        safe["meeting_date"],
        safe["meeting_time"],
        safe["status"],
        safe["sheets"],
    )


def _esc(value: object) -> str:
    """HTML-escape one booking field for the mail template."""
    import html as _html

    return _html.escape(str(value or ""), quote=True)


def confirmation_html(booking: dict) -> str:
    """Polished meeting-confirmation template (inline CSS, no secrets).

    All user-supplied fields are HTML-escaped. Table layout plus inline
    styles keep it readable in plain mail clients with no external CSS.
    """
    name = _esc(booking.get("name"))
    date = _esc(booking.get("meeting_date"))
    time = _esc(booking.get("meeting_time"))
    tz = _esc(booking.get("timezone"))
    topic = _esc(booking.get("topic", "Meeting"))
    meeting_type = _esc(booking.get("meeting_type"))
    booking_id = _esc(booking.get("booking_id"))
    notes = _esc(booking.get("notes"))
    notes_row = (
        f'<tr><td style="padding:8px 12px;color:#5b6577;">Notes</td>'
        f'<td style="padding:8px 12px;color:#20283a;">{notes}</td></tr>'
        if booking.get("notes") else ""
    )
    return (
        '<html><body style="margin:0;padding:24px;background:#f4f6fb;'
        'font-family:Segoe UI,Arial,sans-serif;">'
        '<table width="100%" cellpadding="0" cellspacing="0">'
        '<tr><td align="center">'
        '<table width="520" cellpadding="0" cellspacing="0" '
        'style="background:#ffffff;border:1px solid #e1e5ef;border-radius:12px;'
        'overflow:hidden;">'
        '<tr><td style="padding:20px 24px;background:#3459c9;color:#ffffff;">'
        '<div style="font-size:18px;font-weight:bold;">Meeting confirmed</div>'
        f'<div style="font-size:13px;opacity:0.9;">{topic}</div>'
        "</td></tr>"
        '<tr><td style="padding:16px 24px;color:#20283a;font-size:14px;">'
        f"Thank you, {name}. Your meeting is scheduled for "
        f"<b>{date}</b> at <b>{time}</b> {tz}."
        "</td></tr>"
        '<tr><td style="padding:0 24px 8px;">'
        '<table width="100%" cellpadding="0" cellspacing="0" '
        'style="font-size:13px;border:1px solid #eef1f6;border-radius:8px;">'
        f'<tr><td style="padding:8px 12px;color:#5b6577;">Date</td>'
        f'<td style="padding:8px 12px;color:#20283a;">{date}</td></tr>'
        f'<tr><td style="padding:8px 12px;color:#5b6577;">Time</td>'
        f'<td style="padding:8px 12px;color:#20283a;">{time} {tz}</td></tr>'
        f'<tr><td style="padding:8px 12px;color:#5b6577;">Topic</td>'
        f'<td style="padding:8px 12px;color:#20283a;">{topic}</td></tr>'
        f'<tr><td style="padding:8px 12px;color:#5b6577;">Meeting type</td>'
        f'<td style="padding:8px 12px;color:#20283a;">{meeting_type}</td></tr>'
        f'<tr><td style="padding:8px 12px;color:#5b6577;">Booking ID</td>'
        f'<td style="padding:8px 12px;color:#20283a;font-weight:bold;">{booking_id}</td></tr>'
        f"{notes_row}"
        "</table></td></tr>"
        '<tr><td style="padding:12px 24px 20px;color:#7a86a0;font-size:12px;">'
        "This is an automated confirmation from Raala. "
        "Reply to this email if you need to change anything."
        "</td></tr>"
        "</table></td></tr></table></body></html>"
    )


def confirmation_text(booking: dict) -> str:
    """Plain-text fallback paired with the HTML template."""
    lines = [
        f"Meeting confirmed: {booking.get('topic', 'Meeting')}",
        "",
        f"Thank you, {booking.get('name', '')}. Your meeting is scheduled for "
        f"{booking.get('meeting_date', '')} at {booking.get('meeting_time', '')} "
        f"{booking.get('timezone', '')}.",
        f"Meeting type: {booking.get('meeting_type', '')}.",
        f"Booking ID: {booking.get('booking_id', '')}.",
    ]
    if booking.get("notes"):
        lines.append(f"Notes: {booking.get('notes', '')}")
    return "\n".join(lines)


def confirmation_speech(booking: dict, tomorrow_label: str = "tomorrow") -> str:
    try:
        start = datetime.fromisoformat(str(booking.get("start", "")).replace("Z", "+00:00"))
        when = f"{tomorrow_label}, {spoken_date(start)}, at {spoken_time(start)}"
    except (ValueError, TypeError):
        when = (
            f"{tomorrow_label}, {booking.get('meeting_date', '')}, at "
            f"{booking.get('meeting_time', '')}"
        )
    return (
        f"Thank you, {booking.get('name', '')}. Your meeting is scheduled for "
        f"{when} {booking.get('timezone', '')}. "
        f"The meeting topic is {booking.get('topic', '')}, and the meeting type is "
        f"{booking.get('meeting_type', '')}. A confirmation will be sent to "
        f"{booking.get('email', '')}. Your booking ID is {booking.get('booking_id', '')}."
    )


# ---------------------------------------------------------------------------
# Voice slot-filling: one question at a time, confirm, then book.
# ---------------------------------------------------------------------------

#: Order in which the voice flow collects missing details.
VOICE_FIELD_ORDER: tuple[str, ...] = ("topic", "meeting_type", "name", "email")

VOICE_QUESTIONS: dict[str, str] = {
    "topic": "What is the meeting about?",
    "meeting_type": "Should it be a video call, phone call, or in-person meeting?",
    "name": "What is your full name?",
    "email": "What email should I send the confirmation to?",
}

#: Short labels for naming what is still missing from the form.
VOICE_FIELD_LABELS: dict[str, str] = {
    "topic": "the topic",
    "meeting_type": "the meeting type",
    "name": "your name",
    "email": "an email address",
}

_TYPE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("video call", r"\b(video|zoom|google\s*meet|teams)\b"),
    ("phone call", r"\b(phone|call me)\b"),
    ("in-person", r"\bin[\s-]?person\b|\boffice\b|\bcome in\b"),
)


def parse_meeting_type(text: str) -> str | None:
    """Map free words to a meeting type; video wins over phone ("video call")."""
    lowered = (text or "").lower()
    for meeting_type, pattern in _TYPE_PATTERNS:
        if re.search(pattern, lowered):
            return meeting_type
    return None


def extract_email(text: str) -> str | None:
    """First email-looking address in free text, if any."""
    match = _EMAIL_RE.search(str(text or ""))
    return match.group(0).lower() if match else None


_TIME_REQUEST_RE = re.compile(
    r"\d{1,2}:\d{2}|\d{1,2}\s*(a\.?m\.?|p\.?m\.?)|\bat\s+\d{1,2}\b|\bo'?clock\b",
    re.IGNORECASE,
)


def explicit_time_mentioned(text: str) -> bool:
    """True when the message names a clock time ("10 AM", "at 10", "10:30").

    Plain counts ("2 people", "30 minutes") do not count, so they never
    trigger a taken-slot verdict by accident.
    """
    return bool(_TIME_REQUEST_RE.search(str(text or "")))


def match_slot(
    text: str, free: list[tuple[datetime, datetime]]
) -> tuple[datetime, datetime] | None:
    """Match a spoken hour ("10", "10am", "10:30 AM") to a free slot."""
    for match in re.finditer(r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?", (text or "").lower()):
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        meridiem = (match.group(3) or "").replace(".", "")
        if hour > 12 or minute > 59:
            continue
        hour24 = hour % 12
        if meridiem == "pm" and hour != 12:
            hour24 += 12
        elif meridiem == "am" and hour == 12:
            hour24 = 0
        elif not meridiem:
            if hour == 12:
                hour24 = 12  # Bare "12" means noon, which has a slot.
            elif hour <= 7:
                # Bare "1".."7" in a 9 AM-7 PM day means the afternoon slot.
                hour24 += 12
        for start, end in free:
            if start.hour == hour24 and start.minute == minute:
                return (start, end)
    return None


def next_missing_voice_field(details: dict) -> str | None:
    """First empty voice field in collection order, or None when complete."""
    for key in VOICE_FIELD_ORDER:
        if not str(details.get(key) or "").strip():
            return key
    return None


def confirm_summary(details: dict, day: datetime, start: datetime, tz_label: str) -> str:
    """Exact pre-booking summary the voice agent reads for confirmation."""
    _ = day
    return (
        f"Please confirm: a {details.get('meeting_type', '')} about "
        f"{details.get('topic', '')} on {spoken_date(start)} at "
        f"{spoken_time(start)} in {tz_label}, under the name "
        f"{details.get('name', '')}, using {details.get('email', '')}. "
        "Should I book it?"
    )


def alternative_suggestion(free: list[tuple[datetime, datetime]], count: int = 3) -> str:
    """Up to `count` spoken alternative times for a taken slot."""
    labels = [spoken_time(start) for start, _ in free[: max(count, 1)]]
    if not labels:
        return "There is nothing else free that day."
    if len(labels) == 1:
        return f"The only other free time is {labels[0]}."
    return "Other free times are " + ", ".join(labels[:-1]) + f", and {labels[-1]}."
