"""Gmail service: send-only, matching the granted `gmail.send` scope.

Only `send_email` is exposed. Inbox reading, search, labels, deletion, and
bulk sending are deliberately unsupported — the agent says so instead.
"""

from __future__ import annotations

import base64
import logging
from email.message import EmailMessage

from pydantic import BaseModel

from app.agent import action_models as models
from app.agent.tool_executor import ToolResult
from app.agent.tool_registry import ToolRegistry
from app.integrations.google import oauth
from app.integrations.google import token_store as token_store_module

logger = logging.getLogger(__name__)

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
GMAIL_SEND_SCOPE = oauth.GMAIL_SEND_SCOPE
REQUIRED_SCOPES = (GMAIL_SEND_SCOPE,)

_MISSING_AUTH = (
    "Gmail sending is not authorized yet. "
    "Open /auth/google/login in your browser to grant Gmail access "
    "(your existing Calendar access is preserved)."
)


class GmailAPIError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _check_scope(deps) -> None:
    granted = token_store_module.granted_scopes(deps.token_path)
    if granted and not any(scope in granted for scope in REQUIRED_SCOPES):
        raise PermissionError(_MISSING_AUTH)


async def _request(client, method: str, path: str, token: str, **kwargs) -> dict:
    headers = dict(kwargs.pop("headers", {}))
    headers["Authorization"] = f"Bearer {token}"
    response = await client.request(method, GMAIL_API_BASE + path, headers=headers, **kwargs)
    if response.status_code == 401:
        raise GmailAPIError("auth_expired", "Google authorization expired.", status=401)
    if response.status_code == 403:
        raise PermissionError(_MISSING_AUTH)
    if response.status_code == 429:
        raise GmailAPIError("rate_limited", "Gmail rate limit reached. Try again shortly.", status=429)
    if response.status_code < 200 or response.status_code >= 300:
        raise GmailAPIError(
            "provider_error", f"Gmail returned HTTP {response.status_code}.",
            status=response.status_code,
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise GmailAPIError("provider_error", "Gmail returned invalid data.") from exc
    return body if isinstance(body, dict) else {}


async def call_gmail_api(client, method: str, path: str, ensure_token, **kwargs) -> dict:
    token = await ensure_token()
    try:
        return await _request(client, method, path, token, **kwargs)
    except GmailAPIError as exc:
        if exc.code != "auth_expired":
            raise
    token = await ensure_token(force_refresh=True)
    return await _request(client, method, path, token, **kwargs)


def build_raw_message(to: list[str], subject: str, body: str) -> str:
    """Build a base64url-encoded plain-text MIME message (no attachments)."""
    message = EmailMessage()
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def build_raw_html_message(
    to: list[str], subject: str, text_body: str, html_body: str
) -> str:
    """Build a multipart (plain-text + HTML) MIME message (no attachments)."""
    message = EmailMessage()
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


async def send_email_message(client, ensure_token, to: list[str], subject: str, body: str) -> dict:
    sent = await call_gmail_api(
        client, "POST", "/users/me/messages/send", ensure_token,
        json={"raw": build_raw_message(to, subject, body)},
    )
    return {
        "id": sent.get("id", ""),
        "threadId": sent.get("threadId", ""),
        "to": to,
        "subject": subject,
    }


async def send_email_html_message(
    client, ensure_token, to: list[str], subject: str, text_body: str, html_body: str
) -> dict:
    """Send a multipart plain-text + HTML message (no attachments)."""
    sent = await call_gmail_api(
        client, "POST", "/users/me/messages/send", ensure_token,
        json={"raw": build_raw_html_message(to, subject, text_body, html_body)},
    )
    return {
        "id": sent.get("id", ""),
        "threadId": sent.get("threadId", ""),
        "to": to,
        "subject": subject,
    }


def register_gmail_tools(registry: ToolRegistry, deps) -> None:
    async def _send(validated: BaseModel | dict, context: dict) -> ToolResult | dict:
        assert isinstance(validated, models.SendEmailArgs)
        _check_scope(deps)
        async with deps.client_factory() as client:
            sent = await send_email_message(
                client, deps.ensure_token, validated.to, validated.subject, validated.body
            )
        recipients = ", ".join(validated.to)
        return ToolResult(
            ok=True,
            code="sent",
            message=(
                f"Your email has been sent successfully to {recipients} "
                f"with subject '{validated.subject}'."
            ),
            data=sent,
        )

    registry.register(
        "send_email",
        "Send a plain-text email to explicit recipients immediately. No inbox access.",
        models.SendEmailArgs,
        _send,
    )
