"""Google Calendar service: five strict actions, real API responses only.

All calls run through `call_calendar_api`, which injects the access token,
refreshes once on 401, and maps failures to structured errors. No fabricated
events: empty results are reported as empty.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"


class CalendarAPIError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _iso(value: datetime) -> str:
    return value.isoformat()


def _event_summary(event: dict) -> dict:
    start = event.get("start", {}) or {}
    end = event.get("end", {}) or {}
    return {
        "id": event.get("id", ""),
        "summary": event.get("summary", "(no title)"),
        "start": start.get("dateTime") or start.get("date") or "",
        "end": end.get("dateTime") or end.get("date") or "",
        "location": event.get("location", ""),
        "htmlLink": event.get("htmlLink", ""),
    }


async def _request(client, method: str, path: str, token: str, **kwargs) -> dict:
    headers = dict(kwargs.pop("headers", {}))
    headers["Authorization"] = f"Bearer {token}"
    response = await client.request(method, CALENDAR_API_BASE + path, headers=headers, **kwargs)
    if response.status_code == 401:
        raise CalendarAPIError("auth_expired", "Google authorization expired.", status=401)
    if response.status_code == 403:
        raise PermissionError(
            "An additional Google permission is required. "
            "Open /auth/google/login in your browser to grant it "
            "(existing access is preserved)."
        )
    if response.status_code == 429:
        raise CalendarAPIError("rate_limited", "Google Calendar rate limit reached. Try again shortly.", status=429)
    if response.status_code == 204:
        return {}
    if response.status_code < 200 or response.status_code >= 300:
        raise CalendarAPIError(
            "provider_error",
            f"Google Calendar returned HTTP {response.status_code}.",
            status=response.status_code,
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise CalendarAPIError("provider_error", "Google Calendar returned invalid data.") from exc
    return body if isinstance(body, dict) else {}


async def call_calendar_api(client, method: str, path: str, ensure_token, **kwargs) -> dict:
    """Call the API, refreshing the access token once on expiry."""
    token = await ensure_token()
    try:
        return await _request(client, method, path, token, **kwargs)
    except CalendarAPIError as exc:
        if exc.code != "auth_expired":
            raise
    token = await ensure_token(force_refresh=True)
    return await _request(client, method, path, token, **kwargs)


async def list_events_in_range(
    client, ensure_token, start: datetime, end: datetime, limit: int = 25
) -> list[dict]:
    body = await call_calendar_api(
        client,
        "GET",
        "/calendars/primary/events",
        ensure_token,
        params={
            "timeMin": _iso(start),
            "timeMax": _iso(end),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(max(1, min(limit, 50))),
        },
    )
    items = body.get("items", [])
    if not isinstance(items, list):
        return []
    return [_event_summary(e) for e in items if isinstance(e, dict)]


async def get_today_events(client, ensure_token, now: datetime) -> dict:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events = await list_events_in_range(client, ensure_token, start, start + timedelta(days=1))
    return {"date": start.date().isoformat(), "count": len(events), "events": events}


async def get_upcoming_events(
    client, ensure_token, now: datetime, days: int, limit: int
) -> dict:
    from app.agent.action_models import MAX_UPCOMING_DAYS

    days = max(1, min(days, MAX_UPCOMING_DAYS))
    events = await list_events_in_range(client, ensure_token, now, now + timedelta(days=days), limit)
    return {"days": days, "count": len(events), "events": events[:limit]}


async def check_availability(
    client, ensure_token, start: datetime, end: datetime
) -> dict:
    events = await list_events_in_range(client, ensure_token, start, end)
    return {
        "start": _iso(start),
        "end": _iso(end),
        "free": not events,
        "conflicts": events,
    }


async def get_event_details(client, ensure_token, event_id: str) -> dict:
    body = await call_calendar_api(
        client, "GET", f"/calendars/primary/events/{event_id}", ensure_token
    )
    return _event_summary(body)


async def create_event(
    client,
    ensure_token,
    summary: str,
    start: datetime,
    end: datetime,
    description: str = "",
    time_zone: str = "",
) -> dict:
    payload: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": _iso(start)},
        "end": {"dateTime": _iso(end)},
    }
    if description:
        payload["description"] = description
    zone = time_zone or (start.tzinfo.key if hasattr(start.tzinfo, "key") else "")
    if zone:
        payload["start"]["timeZone"] = zone
        payload["end"]["timeZone"] = zone
    body = await call_calendar_api(
        client, "POST", "/calendars/primary/events", ensure_token, json=payload
    )
    return _event_summary(body)


async def delete_event(client, ensure_token, event_id: str) -> dict:
    """Delete one Calendar event; a missing event counts as already gone."""
    try:
        await call_calendar_api(
            client, "DELETE", f"/calendars/primary/events/{event_id}", ensure_token
        )
    except CalendarAPIError as exc:
        if exc.status != 404:
            raise
    return {"deleted": True, "id": event_id}
