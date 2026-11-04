"""AI drafting for outgoing mail: spoken purpose in, clean draft out.

The user's words describe what the email is for; they are never stored as
the body verbatim. One non-streaming Muse Spark call returns a polished
subject and body as JSON. Any failure raises, and the caller falls back to
the user's raw words so sending never depends on the drafter being up.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from app.agent.selector import _extract_json_object

logger = logging.getLogger(__name__)

DRAFTER_SYSTEM = (
    "You are Raala, a receptionist drafting an email on behalf of the user. "
    "The user will tell you what the email is for, in their own words. "
    "Return ONLY one JSON object and nothing else: no markdown, no code fences.\n"
    'Format: {{"subject": "...", "body": "..."}}\n'
    "Rules:\n"
    "- If a subject is supplied, use it as given (fix obvious case only). "
    "Otherwise write a subject under 8 words.\n"
    "- Body: 2 to 5 short natural sentences that say exactly what the user meant, "
    "in a polite professional tone.\n"
    "- Keep every fact the user gave (names, dates, times, numbers) exactly. "
    "Invent nothing: no placeholders, no bracketed text, no new details.\n"
    "- No email headers, no 'Subject:' line inside the body, no signature beyond "
    "a brief sign-off when natural."
)


async def draft_email(
    post_chat: Callable[[dict], Awaitable[dict]],
    *,
    purpose: str,
    subject: str = "",
    to: list | tuple = (),
) -> dict:
    """One non-streaming call -> {"subject": ..., "body": ...}; raises on failure."""
    purpose = (purpose or "").strip()
    if not purpose:
        raise ValueError("purpose is empty")
    user = f"What the email is for: {purpose}\n"
    if subject:
        user += f"Subject the user already picked: {subject}\n"
    if to:
        user += f"Recipient: {', '.join(str(address) for address in to)}\n"
    payload = {
        "messages": [
            {"role": "developer", "content": DRAFTER_SYSTEM},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "max_completion_tokens": 400,
    }
    response = await post_chat(payload)
    result = _parse_draft_response(response)
    if result is None:
        raise ValueError("drafter returned an unusable response")
    return result


def _parse_draft_response(response: dict) -> dict | None:
    if not isinstance(response, dict):
        return None
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    candidate = _extract_json_object(content.strip())
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    subject = parsed.get("subject")
    if isinstance(subject, str):
        subject = " ".join(subject.split())[:500]
    else:
        subject = ""
    body = parsed.get("body")
    if not isinstance(body, str) or not body.strip():
        return None
    return {"subject": subject, "body": body.strip()[:20000]}
