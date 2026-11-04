"""Per-session email slot-filling drafts (recipient -> subject -> body).

Voice and typed turns rarely carry a complete email, so partial details
accumulate here until the draft validates as a whole. A draft never sends
anything: once complete it moves through the normal confirmation proposal.
Drafts expire quickly and are discarded on cancel or on a new valid intent.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

DRAFT_TTL_SECONDS = 600
EMAIL_ORDER: tuple[str, ...] = ("to", "subject", "body")

SLOT_QUESTIONS: dict[str, str] = {
    "to": "Who should I send it to? You can type the address in the box below.",
    "subject": "What subject should I use?",
    "body": "What should the message say?",
}

_CANCEL_RE = re.compile(
    r"\b(cancel|stop|never mind|nevermind|forget it|abort|drop (it|that|this))\b",
    re.IGNORECASE,
)
_ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SUBJECT_RE = re.compile(r"subject\s*(?:would be|is|:)?\s*(.+)", re.IGNORECASE | re.DOTALL)
_BODY_RE = re.compile(
    r"(?:message|body)\s*(?:would be|is|should say|say|:)?\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)


def is_cancel_text(text: str) -> bool:
    """Return True when the user drops the in-progress request."""
    return bool(_CANCEL_RE.search(text or ""))


def extract_email_slots(raw_args: dict | None, text: str) -> dict:
    """Collect the valid email subset from selector args plus message text."""
    found: dict = {}
    to: list[str] = []
    raw = raw_args or {}
    raw_to = raw.get("to")
    if isinstance(raw_to, str):
        to.extend(_ADDRESS_RE.findall(raw_to))
    elif isinstance(raw_to, (list, tuple)):
        for item in raw_to:
            to.extend(_ADDRESS_RE.findall(str(item)))
    for address in _ADDRESS_RE.findall(text or ""):
        clean = address.rstrip(".,;:!?")
        if clean not in to:
            to.append(clean)
    if to:
        found["to"] = to[:10]
    for key, pattern in (("subject", _SUBJECT_RE), ("body", _BODY_RE)):
        direct = raw.get(key)
        if isinstance(direct, str) and direct.strip():
            found[key] = direct.strip()
            continue
        match = pattern.search(text or "")
        if match and len(match.group(1).strip()) > 1:
            found[key] = match.group(1).strip()
    return found


def fill_awaiting_slot(awaiting: str | None, text: str) -> tuple[str, str | list] | None:
    """Map a short plain reply onto the slot Raala just asked for."""
    clean = (text or "").strip()
    if not clean or len(clean) > 500 or "?" in clean or is_cancel_text(clean):
        return None
    if awaiting == "to":
        found = [address.rstrip(".,;:!?") for address in _ADDRESS_RE.findall(clean)]
        if not found:
            return None
        return ("to", found[:10])
    if awaiting in ("subject", "body"):
        if _ADDRESS_RE.search(clean):
            return None
        return (awaiting, clean)
    return None


_SUBJECT_SPLIT_CUES_RE = re.compile(
    r"\b(saying|says|say|that says?|message(?:\s+is)?|body(?:\s+is)?)\b\s*[:,]?\s*",
    re.IGNORECASE,
)
_SUBJECT_LABEL_RE = re.compile(r"^(my |the )?subject\s*(is|:)?\s*", re.IGNORECASE)
_SUBJECT_LEAD_RE = re.compile(r"^(so|well|ok|okay|and|then)\b\s*", re.IGNORECASE)
_CLAUSE_SPLIT_RE = re.compile(r"\s+(and|then)\s+|[,;.]|\s*:\s*")
_TRAILING_FILLER_RE = re.compile(r"\s+(and|then|uh|um|er|like)\s*$", re.IGNORECASE)


def _clean_subject(raw: str) -> str:
    """Trim labels, filler, and trailing clauses from a dictated subject."""
    text = _SUBJECT_LABEL_RE.sub("", (raw or "").strip())
    text = _SUBJECT_LEAD_RE.sub("", text.strip())
    first = _CLAUSE_SPLIT_RE.split(text, maxsplit=1)[0].strip()
    return _TRAILING_FILLER_RE.sub("", first).strip()


def split_subject_message(text: str) -> dict | None:
    """Split a one-go "subject ... message ..." reply into both slots.

    Uses the LAST cue word ("saying", "says", "message is", ...) so rambling
    dictation keeps its tail as the message. Returns None when there is no
    usable split; the caller keeps the old single-slot behavior then.
    """
    clean = (text or "").strip()
    if not clean:
        return None
    best: tuple[str, str] | None = None
    for match in _SUBJECT_SPLIT_CUES_RE.finditer(clean):
        body = clean[match.end():].strip(" ,:;")
        subj = clean[: match.start()].strip(" ,:;")
        if subj and body:
            best = (subj, body)
    if best is None:
        return None
    subject = _clean_subject(best[0])
    body = best[1].strip()
    if not subject or not body:
        return None
    return {"subject": subject, "body": body}


def next_missing_slot(fields: dict) -> str | None:
    """First empty slot in fill order, or None when the draft is complete."""
    for key in EMAIL_ORDER:
        value = fields.get(key)
        if not value:
            return key
    return None


class EmailDraftStore:
    """In-memory email drafts, keyed by chat session."""

    def __init__(self) -> None:
        self._drafts: dict[str, dict] = {}

    def save(self, session_id: str, fields: dict, awaiting: str | None) -> dict:
        draft = {
            "action": "send_email",
            "fields": {key: fields[key] for key in EMAIL_ORDER if fields.get(key)},
            "awaiting": awaiting,
            "updated_at": time.time(),
        }
        self._drafts[session_id] = draft
        return draft

    def get_valid(self, session_id: str) -> dict | None:
        draft = self._drafts.get(session_id)
        if draft is None:
            return None
        if time.time() - draft["updated_at"] > DRAFT_TTL_SECONDS:
            self._drafts.pop(session_id, None)
            return None
        return draft

    def discard(self, session_id: str) -> None:
        self._drafts.pop(session_id, None)


# ---------------------------------------------------------------------------
# Meeting slot-filling (title -> start -> end).
# ---------------------------------------------------------------------------

MEETING_ORDER: tuple[str, ...] = ("summary", "start", "end")
MEETING_TTL_SECONDS = 600

MEETING_QUESTIONS: dict[str, str] = {
    "summary": "What should I call the meeting?",
    "start": "When does it start? For example, tomorrow at 10 AM.",
    "end": "When does it end? Say a time like 11 AM or a length like one hour.",
}

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

_WEEKDAY_RE = re.compile(
    r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"|\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_MONTH_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})\b",
    re.IGNORECASE,
)
_TIME_RE = re.compile(
    r"(half past|quarter past|quarter to)\s+(\d{1,2})\b"
    r"|(?:at\s+|@\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?"
    r"|\b(noon|midnight)\b",
    re.IGNORECASE,
)
_DAYPART_PM_RE = re.compile(r"\b(afternoon|evening|night|tonight)\b", re.IGNORECASE)
_RANGE_OR_AT_RE = re.compile(r"(?:\bat\b|@|to|till|until|through|[-–—])\s*$", re.IGNORECASE)
_DATE_CUE_RE = re.compile(
    r"\b(today|tonight|tomorrow|day after tomorrow|next|monday|tuesday|wednesday"
    r"|thursday|friday|saturday|sunday|january|february|march|april|may|june|july"
    r"|august|september|october|november|december|morning|afternoon|evening|night"
    r"|noon|midnight|o'?clock|[ap]\.?m\.?)\b|\d",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?)\b", re.IGNORECASE)
_HALF_HOUR_RE = re.compile(r"\b(half an hour|an? half[\s-]?hour|half[\s-]?hour)\b", re.IGNORECASE)
_AN_HOUR_RE = re.compile(r"\ban?\s+hour\b", re.IGNORECASE)


def has_datetime_cue(text: str) -> bool:
    """Return True when the text carries any date or time hint."""
    return bool(_DATE_CUE_RE.search(text or ""))


def _find_times(text: str) -> list[tuple[int, int, bool, bool]]:
    """All clock times left to right as (hour24, minute, explicit, meridiem_given)."""
    found: list[tuple[int, int, bool, bool]] = []
    lowered = (text or "").lower()
    pm_hint = bool(_DAYPART_PM_RE.search(lowered))
    for match in _TIME_RE.finditer(text or ""):
        phrase, phrase_hr = match.group(1), match.group(2)
        if phrase:
            hour = int(phrase_hr)
            if hour < 1 or hour > 12:
                continue
            if "past" in phrase:
                minute = 30 if "half" in phrase else 15
                hour24 = hour % 12
            else:  # quarter to
                minute = 45
                hour24 = (hour - 1) % 12
            if pm_hint and hour24 < 12:
                hour24 += 12
            found.append((hour24, minute, True, True))
            continue
        word = match.group(6)
        if word:
            found.append((12, 0, True, True) if word.lower() == "noon" else (0, 0, True, True))
            continue
        hour = int(match.group(3))
        minute = int(match.group(4)) if match.group(4) else 0
        meridiem = (match.group(5) or "").lower().replace(".", "")
        if hour > 23 or minute > 59:
            continue
        before = text[max(0, match.start() - 8): match.start()]
        after = text[match.end(): match.end() + 8].lower()
        at_near = bool(_RANGE_OR_AT_RE.search(before))
        oclock = "o'clock" in after or "oclock" in after
        explicit = bool(meridiem or match.group(4) or at_near or oclock)
        hour24 = hour
        if meridiem == "pm" and hour < 12:
            hour24 = hour + 12
        elif meridiem == "am" and hour == 12:
            hour24 = 0
        elif not meridiem and pm_hint and hour < 12:
            hour24 = hour + 12
        found.append((hour24, minute, explicit, bool(meridiem)))
    return found


def parse_meeting_start(text: str, now: datetime | None = None) -> datetime | None:
    """Parse a meeting start from voice text; None when no usable date+time."""
    clean = (text or "").strip()
    if not clean:
        return None
    now = now or datetime.now().astimezone()
    lowered = clean.lower()
    today = now.date()
    dated = True
    explicit_today = False
    weekday_shift = False
    if "day after tomorrow" in lowered:
        day = today + timedelta(days=2)
    elif "tomorrow" in lowered:
        day = today + timedelta(days=1)
    elif "tonight" in lowered or re.search(r"\btoday\b", lowered):
        day = today
        explicit_today = True
    else:
        month = _MONTH_RE.search(lowered)
        weekday = _WEEKDAY_RE.search(lowered)
        if month:
            month_num = _MONTHS[month.group(1)[:3].lower()]
            try:
                day = datetime(now.year, month_num, int(month.group(2))).date()
            except ValueError:
                return None
            if day < today:
                try:
                    day = datetime(now.year + 1, month_num, int(month.group(2))).date()
                except ValueError:
                    return None
        elif weekday:
            name = (weekday.group(2) or weekday.group(1).split()[-1]).lower()
            delta = (_WEEKDAYS[name] - today.weekday()) % 7
            day = today + timedelta(days=delta)
            if delta == 0:
                weekday_shift = True
        else:
            day = today
            dated = False
    times = [entry for entry in _find_times(clean) if entry[2] or dated]
    if not times:
        return None
    moment = datetime(day.year, day.month, day.day, times[0][0], times[0][1], tzinfo=now.tzinfo)
    if moment <= now and not explicit_today:
        moment += timedelta(days=7 if weekday_shift else 1)
    return moment


def parse_meeting_end(text: str, start: datetime | None, now: datetime | None = None) -> datetime | None:
    """Parse a meeting end: duration, range end, or bare time on the start date."""
    clean = (text or "").strip()
    if not clean or start is None:
        return None
    lowered = clean.lower()
    if _HALF_HOUR_RE.search(lowered):
        return start + timedelta(minutes=30)
    if _AN_HOUR_RE.search(lowered):
        return start + timedelta(minutes=60)
    duration = _DURATION_RE.search(lowered)
    if duration:
        amount = float(duration.group(1))
        unit = duration.group(2).lower()
        minutes = amount * 60 if unit.startswith("hour") or unit.startswith("hr") else amount
        return start + timedelta(minutes=minutes)
    for hour24, minute, explicit, ap_given in _find_times(clean):
        if not explicit:
            continue
        moment = datetime(start.year, start.month, start.day, hour24, minute, tzinfo=start.tzinfo)
        if moment == start:
            continue
        if moment < start and not ap_given:
            moment += timedelta(hours=12)
        return moment
    for hour24, minute, _explicit, _ap in _find_times(clean):
        moment = datetime(start.year, start.month, start.day, hour24, minute, tzinfo=start.tzinfo)
        if moment == start:
            continue
        if moment < start:
            moment += timedelta(hours=12)
        if moment == start:
            continue
        return moment
    return None


_MEETING_INTENT_RE = re.compile(r"^(i want to|i'd like to|please|can you|could you|would you)\s+", re.IGNORECASE)
_MEETING_LABEL_RE = re.compile(
    r"^(schedule|set up|set|book|arrange|create|add|plan)\s+(a|an|the|my)\s+"
    r"(meeting|appointment|call|event)\b\s*(for me|please)?\s*",
    re.IGNORECASE,
)
_MEETING_ABOUT_RE = re.compile(
    r"^(the|my)\s+meeting\s+(is\s+|about\s+|called\s+|to discuss\s+|regarding\s+)?",
    re.IGNORECASE,
)
_MEETING_CALLED_RE = re.compile(r"^(called|named|titled|about|regarding)\s+", re.IGNORECASE)
_MEETING_TRAIL_RE = re.compile(r"\s+(and|then|uh|um|er)\s*$", re.IGNORECASE)


def clean_meeting_summary(text: str) -> str:
    """Trim intent verbs and labels from a dictated meeting title."""
    cleaned = _MEETING_INTENT_RE.sub("", (text or "").strip()).strip()
    stripped = _MEETING_LABEL_RE.sub("", cleaned).strip()
    if not stripped:
        return ""
    stripped = _MEETING_ABOUT_RE.sub("", stripped).strip()
    stripped = _MEETING_CALLED_RE.sub("", stripped).strip()
    stripped = _MEETING_TRAIL_RE.sub("", stripped).strip()
    return re.sub(r"\s+", " ", stripped)


def next_missing_meeting_slot(fields: dict) -> str | None:
    """First empty slot in fill order, or None when the draft is complete."""
    for key in MEETING_ORDER:
        if not fields.get(key):
            return key
    return None


class MeetingDraftStore:
    """In-memory meeting drafts, keyed by chat session (mirrors EmailDraftStore)."""

    def __init__(self) -> None:
        self._drafts: dict[str, dict] = {}

    def save(self, session_id: str, fields: dict, awaiting: str | None) -> dict:
        draft = {
            "action": "create_calendar_event",
            "fields": {key: fields[key] for key in MEETING_ORDER if fields.get(key)},
            "awaiting": awaiting,
            "updated_at": time.time(),
        }
        self._drafts[session_id] = draft
        return draft

    def get_valid(self, session_id: str) -> dict | None:
        draft = self._drafts.get(session_id)
        if draft is None:
            return None
        if time.time() - draft["updated_at"] > MEETING_TTL_SECONDS:
            self._drafts.pop(session_id, None)
            return None
        return draft

    def discard(self, session_id: str) -> None:
        self._drafts.pop(session_id, None)


def merge_meeting_fields(
    fields: dict, raw_args: dict | None = None, text: str = "", now: datetime | None = None
) -> tuple[dict, bool]:
    """Merge selector args plus free text into meeting fields.

    Only missing slots are filled from text, so a short "11" never clobbers
    an already known start. Returns the fields and whether anything changed.
    """
    before = dict(fields)
    raw = raw_args or {}
    for key in MEETING_ORDER:
        value = raw.get(key)
        if value:
            fields[key] = value
    now = now or datetime.now().astimezone()
    if "summary" not in fields and not has_datetime_cue(text):
        summary = clean_meeting_summary(text)
        if summary:
            fields["summary"] = summary
    if "start" not in fields:
        start = parse_meeting_start(text, now)
        if start is not None:
            fields["start"] = start.isoformat()
    if "end" not in fields and "start" in fields:
        try:
            start_value = fields["start"]
            start_dt = (
                datetime.fromisoformat(start_value.replace("Z", "+00:00"))
                if isinstance(start_value, str)
                else start_value
            )
            end = parse_meeting_end(text, start_dt)
        except (ValueError, TypeError):
            end = None
        if end is not None:
            fields["end"] = end.isoformat()
    return fields, fields != before
