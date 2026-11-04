"""Calendar action selection through Muse Spark.

Two cheap gates protect API credits and latency:

1. Keyword prefilter: ordinary chat never triggers an LLM selection call.
2. Native OpenAI-style `tools` on a non-streaming call; if the provider or
   key does not support tool calling, fall back to validated-JSON parsing,
   then to normal chat. Anything unparseable means "no tool".
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Cheap prefilter; only these messages reach the selection LLM call.
CALENDAR_KEYWORDS: frozenset[str] = frozenset(
    {
        "meeting",
        "meetings",
        "calendar",
        "schedule",
        "scheduled",
        "appointment",
        "appointments",
        "agenda",
        "free",
        "busy",
        "available",
        "availability",
        "book",
        "meet",
        "setup",
    }
)

SELECTOR_SYSTEM = (
    "You are Raala's action router. Output ONLY one JSON object and nothing else: "
    "no greeting, no explanation, no markdown, no code fences.\n"
    "Decide whether the latest user message needs a Google Calendar, Gmail, or Google Tasks action.\n"
    "The endpoint does not support function calling: your JSON text is the whole decision.\n"
    'If yes: {{"action": "<name>", "arguments": {{<args>}}}} using only these actions: '
    "get_today_events (no arguments), "
    "get_upcoming_events {{\"days\": 1-90, \"limit\": 1-50}}, "
    "check_calendar_availability {{\"start\": ISO datetime, \"end\": ISO datetime}}, "
    "get_event_details {{\"event_id\": string}}, "
    "create_calendar_event {{\"summary\": string, \"start\": ISO datetime, \"end\": ISO datetime}}, "
    "send_email {{\"to\": [email, ...], \"subject\": string, \"body\": string}} (send-only; no inbox reading), "
    "list_task_lists (no arguments), "
    "get_tasks {{\"task_list_id\": string, \"max_results\": 1-100}}, "
    "get_task_details {{\"task_id\": string}}, "
    "create_task {{\"title\": string, optional \"notes\", \"due\": ISO datetime}}, "
    "update_task {{\"task_id\": string, optional \"title\", \"notes\", \"due\", \"status\"}}, "
    "complete_task {{\"task_id\": string}}.\n"
    'If the request needs an operation with no matching action (delete, share, modify calendars, read inbox or mail search, labels, sheets, etc.): {{"action": "unsupported"}}.\n'
    'Otherwise: {{"action": "none"}}.\n'
    "Resolve relative dates (today, tomorrow, next week, weekday names) against the provided local time and "
    "always emit ISO datetimes with the local UTC offset. Never invent event IDs or times not stated or implied. "
    "Vague intents still select the action with whatever arguments are known, even if empty. Examples (user message -> your output): "
    "'What meetings do I have today?' -> {{\"action\": \"get_today_events\", \"arguments\": {{}}}}; "
    "'I want to send an email.' -> {{\"action\": \"send_email\", \"arguments\": {{}}}}; "
    "'Remind me to pay the bill tomorrow.' -> {{\"action\": \"create_task\", \"arguments\": {{\"title\": \"pay the bill\"}}}} (plus \"due\" resolved from the local time); "
    "'Thanks!' -> {{\"action\": \"none\"}}."
)


def looks_calendar_related(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in CALENDAR_KEYWORDS)


TOOL_KEYWORDS: frozenset[str] = frozenset(
    {
        "email",
        "mail",
        "gmail",
        "send",
        "draft",
        "compose",
        "write",
        "task",
        "tasks",
        "todo",
        "todos",
        "remind",
        "reminder",
    }
)


def looks_tool_related(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in CALENDAR_KEYWORDS | TOOL_KEYWORDS)


async def select_action(
    post_chat: Callable[[dict], Awaitable[dict]],
    messages: list[dict[str, str]],
    local_now_label: str,
) -> dict[str, Any]:
    """Run the selection call; always return a safe dict, never raise."""
    payload = {
        "messages": [
            {
                "role": "developer",
                "content": SELECTOR_SYSTEM + f"\nLocal time: {local_now_label}.",
            },
            *messages[-6:],
        ],
        "stream": False,
        "max_completion_tokens": 256,
    }
    try:
        response = await post_chat(payload)
    except Exception as exc:
        logger.warning("Calendar action selection failed (%s); using normal chat.", type(exc).__name__)
        return {"action": "none"}
    try:
        return _parse_selection_response(response)
    except Exception:
        logger.warning("Could not parse action selection; using normal chat.")
        return {"action": "none"}


def _parse_selection_response(response: dict) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {"action": "none"}
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return {"action": "none"}
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return {"action": "none"}
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        first = tool_calls[0]
        function = first.get("function", {}) if isinstance(first, dict) else {}
        name = function.get("name")
        raw_args = function.get("arguments", {})
        if isinstance(name, str) and name:
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
            except (ValueError, TypeError, AttributeError):
                args = {}
            if not isinstance(args, dict):
                args = {}
            return {"action": name, "arguments": args}
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return _parse_json_selection(content)
    return {"action": "none"}


def _extract_json_object(text: str) -> str | None:
    """Return the first balanced {...} block, tolerating prose around it."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        start = text.find("{", start + 1)
    return None


def _parse_json_selection(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    candidate = _extract_json_object(text)
    if candidate is None:
        return {"action": "none"}
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return {"action": "none"}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("action"), str):
        return {"action": "none"}
    args = parsed.get("arguments", {})
    return {"action": parsed["action"], "arguments": args if isinstance(args, dict) else {}}
