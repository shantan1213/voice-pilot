"""Google OAuth 2.0 for Raala's Google agent tools (Calendar, Gmail, Tasks).

Web-application flow against the local redirect URI. Calendar, Gmail send, and Tasks scopes
is requested in this phase. Tokens never leave the backend: the frontend only
sees redirects and a connected/not-connected flag.
"""

from __future__ import annotations

import secrets
import time
import urllib.parse

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"

#: Calendar, Gmail send, and Tasks scopes are requested; Sheets stays for later.
REQUESTED_SCOPES = " ".join([CALENDAR_SCOPE, CALENDAR_WRITE_SCOPE, GMAIL_SEND_SCOPE, TASKS_SCOPE])

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

#: Login `state` values are single-use and short-lived.
STATE_TTL_SECONDS = 600

_state_store: dict[str, float] = {}


def create_state() -> str:
    """Create a single-use login state token."""
    _prune_states()
    state = secrets.token_urlsafe(32)
    _state_store[state] = time.time() + STATE_TTL_SECONDS
    return state


def consume_state(state: str | None) -> bool:
    """Validate and consume a state token; rejects missing/reused/expired."""
    if not state:
        return False
    _prune_states()
    expiry = _state_store.pop(state, None)
    return expiry is not None and expiry >= time.time()


def _prune_states() -> None:
    now = time.time()
    expired = [key for key, expiry in _state_store.items() if expiry < now]
    for key in expired:
        _state_store.pop(key, None)


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": REQUESTED_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


async def exchange_code(
    client, client_id: str, client_secret: str, redirect_uri: str, code: str
) -> dict:
    """Exchange an authorization code for tokens (httpx client injected)."""
    response = await client.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code": code,
        },
        timeout=20,
    )
    return _checked_token_response(response)


async def refresh_access_token(client, client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Renew an access token with the stored refresh token."""
    response = await client.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=20,
    )
    return _checked_token_response(response)


def _checked_token_response(response) -> dict:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code < 200 or response.status_code >= 300:
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("error_description") or body.get("error") or "")
        raise RuntimeError(f"Google OAuth failed (HTTP {response.status_code}). {detail}".strip())
    if not isinstance(body, dict) or "access_token" not in body:
        raise RuntimeError("Google OAuth returned an unexpected response.")
    return body
