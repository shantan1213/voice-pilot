"""Voice-Pilot backend for streaming speech, chat, and local TTS."""

import asyncio
import json
import logging
import os
import io
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Literal
from uuid import UUID

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed

from app.conversation_logs import ConversationLogWorker

from app.agent.confirmation import ConfirmationStore
from app.agent.drafts import EmailDraftStore, MeetingDraftStore
from app.agent.tool_registry import ToolRegistry

load_dotenv()

try:
    from app.settings import settings as _app_settings

    _SETTINGS_CHAT_MODEL = _app_settings.muse_chat_model
    _SETTINGS_API_KEY = _app_settings.model_api_key
    _SETTINGS_GOOGLE_CLIENT_ID = _app_settings.google_client_id
    _SETTINGS_GOOGLE_CLIENT_SECRET = _app_settings.google_client_secret
    _SETTINGS_GOOGLE_REDIRECT_URI = _app_settings.google_redirect_uri
except Exception:
    _app_settings = None
    _SETTINGS_CHAT_MODEL = "muse-spark-1.3"
    _SETTINGS_API_KEY = ""
    _SETTINGS_GOOGLE_CLIENT_ID = ""
    _SETTINGS_GOOGLE_CLIENT_SECRET = ""
    _SETTINGS_GOOGLE_REDIRECT_URI = "http://127.0.0.1:8002/auth/google/callback"

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
ASR_URI = "wss://api.meta.ai/v1/asr/realtime"
ASR_MODEL = "muse-voice-transcribe-1.0"
ASR_SAMPLE_RATE = 24_000
ASR_MAX_SESSION_SECONDS = 600
ASR_MAX_FRAME_BYTES = 64 * 1024
ASR_MAX_SESSION_BYTES = ASR_SAMPLE_RATE * 2 * ASR_MAX_SESSION_SECONDS
CHAT_COMPLETIONS_URL = "https://api.meta.ai/v1/chat/completions"
CHAT_MODEL = os.getenv("MUSE_CHAT_MODEL", _SETTINGS_CHAT_MODEL).strip()
CHAT_MAX_MESSAGES = 40
CHAT_MAX_TOTAL_CHARACTERS = 24_000
CHAT_TIMEOUT = httpx.Timeout(connect=10, read=45, write=20, pool=10)
CHAT_LIMITS = httpx.Limits(max_keepalive_connections=10, max_connections=10)
ASR_PING_INTERVAL = 20
ASR_PING_TIMEOUT = 20
CONVERSATION_LOG_DIR = BASE_DIR / "conversation_logs"
conversation_logs = ConversationLogWorker(CONVERSATION_LOG_DIR)
tts_warmup_task: asyncio.Task[None] | None = None
_chat_client: httpx.AsyncClient | None = None
_tts_semaphore: asyncio.Semaphore | None = None
_confirmations_store: ConfirmationStore | None = None
_email_drafts: EmailDraftStore | None = None
_meeting_drafts: MeetingDraftStore | None = None
_voice_bookings: dict[str, dict] = {}
_meeting_offered: dict[str, float] = {}
_last_bookings: dict[str, dict] = {}
VOICE_BOOKING_TTL_SECONDS = 600


def _google_connected() -> bool:
    """True when a Google account is linked (booking requires it)."""
    try:
        from app.integrations.google import token_store

        deps = _google_deps()
        return deps.configured() and token_store.is_connected(deps.token_path)
    except Exception:
        return False


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global tts_warmup_task, _chat_client, _tts_semaphore

    await conversation_logs.start()
    await conversation_logs.clear()
    _chat_client = httpx.AsyncClient(timeout=CHAT_TIMEOUT, limits=CHAT_LIMITS)
    _tts_semaphore = asyncio.Semaphore(2)
    from app.tts import warm_mms_model

    async def warm_tts_in_background() -> None:
        try:
            await asyncio.to_thread(warm_mms_model)
        except Exception:
            logger.exception("MMS-TTS warm-up failed; it will retry on the first voice response.")

    tts_warmup_task = asyncio.create_task(warm_tts_in_background())
    try:
        yield
    finally:
        if tts_warmup_task is not None and not tts_warmup_task.done():
            tts_warmup_task.cancel()
            await asyncio.gather(tts_warmup_task, return_exceptions=True)
        tts_warmup_task = None
        _tts_semaphore = None
        if _chat_client is not None:
            try:
                await _chat_client.aclose()
            except Exception:
                logger.warning("Could not close the shared Muse Spark client.")
            _chat_client = None
        await conversation_logs.stop()


@asynccontextmanager
async def _chat_client_context():
    """Yield the shared chat client, or a fresh one when lifespan is inactive.

    Sharing one client across turns reuses connections and removes TLS plus
    pool setup from the latency path. Tests calling the streamer without
    lifespan still get a fresh patched client.
    """
    shared = _chat_client
    if shared is not None:
        yield shared
        return
    async with httpx.AsyncClient(timeout=CHAT_TIMEOUT, limits=CHAT_LIMITS) as fresh:
        yield fresh


app = FastAPI(title="Voice-Pilot", version="1.0.0", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class TTSRequest(BaseModel):
    """Request body for local text-to-speech."""

    text: str = Field(min_length=1, max_length=1000)
    model: Literal["mms"] = "mms"


class ChatMessage(BaseModel):
    """A complete turn sent as context to Muse Spark."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8_000)


class ChatRequest(BaseModel):
    """Conversation context for one streamed Muse Spark response."""

    messages: list[ChatMessage] = Field(min_length=1, max_length=CHAT_MAX_MESSAGES)
    session_id: str | None = None
    chat_mode: bool = False


class ConversationTurnRequest(BaseModel):
    """A completed, uniquely identified conversation turn to log."""

    session_id: UUID
    turn_id: UUID
    user: str = Field(min_length=1, max_length=8_000)
    assistant: str = Field(min_length=1, max_length=8_000)


@app.get("/api/health")
def health() -> dict:
    """Liveness check for the backend."""
    try:
        from app.tts import device_name, is_model_ready

        model_ready = is_model_ready()
        tts_device = device_name()
    except Exception:
        model_ready = False
        tts_device = "unknown"
    return {"status": "ok", "phase": 1, "app": "Voice-Pilot", "model_ready": model_ready,
            "tts_device": tts_device}


def _sse_event(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=True)}\n\n"


def _upstream_error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        body = {}
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
        else:
            message = body.get("detail")
        if isinstance(message, str) and message.strip():
            return message.strip()[:500]
    return f"Muse Spark returned HTTP {response.status_code}."


async def _stream_muse_chat(messages: list[dict[str, str]], api_key: str) -> AsyncIterator[str]:
    """Stream text deltas from Meta's OpenAI-compatible Chat Completions API."""
    auth_header = "Bearer " + api_key if not api_key.lower().startswith("bearer ") else api_key
    headers = {
        "Accept": "text/event-stream",
        "Authorization": auth_header,
    }
    payload = {
        "model": CHAT_MODEL,
        "messages": messages,
        "stream": True,
        "max_completion_tokens": 1024,
    }
    for attempt in range(2):
        has_text = False
        stream_finished = False
        empty_choices = 0

        try:
            async with _chat_client_context() as client:
                async with client.stream(
                    "POST",
                    CHAT_COMPLETIONS_URL,
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status_code < 200 or response.status_code >= 300:
                        await response.aread()
                        yield _sse_event(
                            {"type": "error", "message": _upstream_error_message(response)}
                        )
                        return

                    response_headers = getattr(response, "headers", {})
                    content_type = response_headers.get("content-type", "")
                    event_count = 0
                    first_chunk_keys: tuple[str, ...] = ()

                    async def process_event(raw_data: str) -> AsyncIterator[str]:
                        nonlocal has_text, stream_finished, empty_choices, event_count, first_chunk_keys
                        if raw_data == "[DONE]":
                            stream_finished = True
                            return
                        try:
                            chunk = json.loads(raw_data)
                        except json.JSONDecodeError:
                            logger.warning("Muse Spark sent a malformed streaming event.")
                            return
                        if not isinstance(chunk, dict):
                            return
                        event_count += 1
                        if not first_chunk_keys:
                            first_chunk_keys = tuple(sorted(chunk.keys()))
                        if isinstance(chunk.get("error"), dict):
                            message = chunk["error"].get("message")
                            yield _sse_event(
                                {
                                    "type": "error",
                                    "message": message if isinstance(message, str) else "Muse Spark returned an error.",
                                }
                            )
                            return

                        choices = chunk.get("choices")
                        if not isinstance(choices, list) or not choices:
                            empty_choices += 1
                            return
                        choice = choices[0]
                        if not isinstance(choice, dict):
                            empty_choices += 1
                            return
                        delta = choice.get("delta")
                        content = delta.get("content") if isinstance(delta, dict) else None
                        if not isinstance(content, str) or not content:
                            message = choice.get("message")
                            if isinstance(message, dict):
                                content = message.get("content")
                        if not isinstance(content, str) or not content:
                            content = choice.get("text")
                        if isinstance(content, str) and content:
                            has_text = True
                            yield _sse_event({"type": "delta", "text": content})
                        if choice.get("finish_reason") is not None:
                            stream_finished = True

                    data_lines: list[str] = []
                    async for line in response.aiter_lines():
                        if line and not data_lines and line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                            continue
                        if line != "" or not data_lines:
                            continue

                        raw_data = "\n".join(data_lines)
                        data_lines.clear()
                        async for event in process_event(raw_data):
                            yield event
                        if stream_finished:
                            break

                    if data_lines and not stream_finished:
                        async for event in process_event("\n".join(data_lines)):
                            yield event

            if has_text and stream_finished:
                yield _sse_event({"type": "done"})
                return
            if has_text:
                yield _sse_event(
                    {"type": "error", "message": "The response stream ended before Muse Spark finished."}
                )
                return
            if attempt == 0:
                logger.warning(
                    "Muse Spark returned an empty successful stream; retrying once "
                    "(%d empty events, content type %s, first keys %s).",
                    empty_choices,
                    content_type or "unknown",
                    first_chunk_keys or ("none",),
                )
                continue
            logger.error(
                "Muse Spark returned an empty successful stream after retry "
                "(%d empty events, %d events, content type %s, first keys %s).",
                empty_choices,
                event_count,
                content_type or "unknown",
                first_chunk_keys or ("none",),
            )
            fallback = await _request_muse_chat_once(messages, api_key)
            if fallback is not None:
                yield _sse_event({"type": "delta", "text": fallback})
                yield _sse_event({"type": "done"})
                return
            yield _sse_event(
                {
                    "type": "error",
                    "message": "Muse Spark returned no text. The provider may be temporarily unavailable.",
                }
            )
            return
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            yield _sse_event({"type": "error", "message": "Muse Spark timed out before finishing the response."})
            return
        except httpx.RequestError as exc:
            logger.warning("Muse Spark request failed (%s).", type(exc).__name__)
            yield _sse_event({"type": "error", "message": "Could not reach Muse Spark."})
            return
        except Exception as exc:
            logger.exception("Muse Spark stream failed (%s).", type(exc).__name__)
            yield _sse_event({"type": "error", "message": "Muse Spark could not complete the response."})
            return


async def _request_muse_chat_once(messages: list[dict[str, str]], api_key: str) -> str | None:
    """Try a non-streaming completion when streaming returned no usable text."""
    async with _chat_client_context() as client:
        if not hasattr(client, "post"):
            return None
        headers = {
            "Accept": "application/json",
            "Authorization": (
                api_key if api_key.lower().startswith("bearer ") else "Bearer " + api_key
            ),
        }
        response = await client.post(
            CHAT_COMPLETIONS_URL,
            headers=headers,
            json={
                "model": CHAT_MODEL,
                "messages": messages,
                "stream": False,
                "max_completion_tokens": 1024,
            },
        )
        if response.status_code < 200 or response.status_code >= 300:
            logger.warning(
                "Muse Spark non-streaming fallback returned HTTP %s.",
                response.status_code,
            )
            return None
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            logger.warning("Muse Spark non-streaming fallback returned invalid JSON.")
            return None
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            logger.warning("Muse Spark non-streaming fallback returned no choices.")
            return None
        choice = choices[0]
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            content = choice.get("text")
        return content.strip() if isinstance(content, str) and content.strip() else None


# ---------------------------------------------------------------------------
# Raala agent tools (Google Calendar is the first; Gmail/Tasks/Sheets can
# register here later without touching the voice pipeline above).
# The gate below only runs for calendar-looking messages, and every action
# is validated by `app.agent` before any Google API call. Muse Spark never
# sees credentials or tokens.
# ---------------------------------------------------------------------------

def _google_deps():
    """Build Calendar dependencies from env, falling back to settings."""
    from app.integrations.google.tools import GoogleDeps

    def _value(env_name: str, setting_name: str, default: str = "") -> str:
        value = os.getenv(env_name, "").strip()
        if value:
            return value
        if _app_settings is not None:
            return str(getattr(_app_settings, setting_name, "") or "").strip()
        return default

    return GoogleDeps(
        client_id=_value("GOOGLE_CLIENT_ID", "google_client_id"),
        client_secret=_value("GOOGLE_CLIENT_SECRET", "google_client_secret"),
        redirect_uri=_value(
            "GOOGLE_REDIRECT_URI",
            "google_redirect_uri",
            "http://127.0.0.1:8002/auth/google/callback",
        ),
    )


def _agent_registry() -> ToolRegistry:
    """Fresh registry per request so tests can patch configuration freely."""
    from app.integrations.google import gmail as gmail_tools
    from app.integrations.google import tasks as tasks_tools
    from app.integrations.google.tools import register_calendar_tools

    registry = ToolRegistry()
    register_calendar_tools(registry, _google_deps())
    gmail_tools.register_gmail_tools(registry, _google_deps())
    tasks_tools.register_tasks_tools(registry, _google_deps())
    return registry


def _confirmation_store() -> ConfirmationStore:
    global _confirmations_store
    if _confirmations_store is None:
        _confirmations_store = ConfirmationStore()
    return _confirmations_store


async def _selector_post(payload: dict, api_key: str) -> dict:
    """Non-streaming Muse Spark call used only for calendar action selection."""
    headers = {
        "Authorization": "Bearer " + api_key
        if not api_key.lower().startswith("bearer ")
        else api_key,
    }
    async with _chat_client_context() as client:
        response = await client.post(
            CHAT_COMPLETIONS_URL,
            headers=headers,
            json={"model": CHAT_MODEL, **payload},
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"Selection call returned HTTP {response.status_code}.")
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Selection call returned invalid data.")
        return data


def _sse_text(text: str) -> list[str]:
    """Deterministic single-message reply (no LLM call, exact wording)."""
    return [_sse_event({"type": "delta", "text": text}), _sse_event({"type": "done"})]


def _progress_text(action: str) -> str:
    """Short spoken progress line shown and said while the API call runs."""
    if action == "send_email":
        return "Sending your mail now. I'll confirm as soon as it's sent."
    if action == "create_calendar_event":
        return "Scheduling your meeting now. I'll confirm as soon as it's saved."
    if action in ("create_task", "update_task", "complete_task"):
        return "Updating your tasks now. I'll confirm as soon as it's done."
    return "Working on it now. I'll confirm as soon as it's done."


# NOTE: @app.post("/api/chat/stream") is attached to chat_stream below.
async def _execute_confirmed(pending, spans=None):
    """Yield a progress update, then run a consumed confirmation against the real Google API.

    The progress delta streams before the API call is awaited, so the browser
    shows and says "sending" in parallel with the send.
    """
    import time

    from pydantic import BaseModel

    from app.agent.action_models import Selection
    from app.agent.tool_executor import execute_tool

    timings = spans if spans is not None else {}

    try:
        validated = Selection(action=pending.action, arguments=pending.arguments).validated_args()
    except Exception:
        for event in _sse_text("That confirmation is no longer valid. Please ask again."):
            yield event
        return
    yield _sse_event({"type": "delta", "text": _progress_text(pending.action)})
    registry = _agent_registry()
    raw = validated.model_dump(mode="json") if isinstance(validated, BaseModel) else {}
    exec_start = time.perf_counter()
    result = await execute_tool(
        registry, pending.action, raw,
        {"session_id": pending.session_id, "confirmed": True},
    )
    timings["exec_ms"] = round((time.perf_counter() - exec_start) * 1000)
    if not result.ok:
        for event in _sse_text(f"I couldn't do that: {result.message}"):
            yield event
        return
    for event in _sse_text(_confirmed_success_text(pending.action, validated)):
        yield event


def _confirmed_success_text(action: str, validated) -> str:
    """Deterministic success wording per confirmed action."""
    if action == "create_calendar_event":
        start = validated.start.strftime("%A, %Y-%m-%d at %H:%M").strip()
        return (
            f"Your meeting has been scheduled successfully: '{validated.summary}' on {start}."
        )
    if action == "send_email":
        recipients = ", ".join(validated.to)
        return (
            f"Your email has been sent successfully to {recipients} "
            f"with subject '{validated.subject}'."
        )
    if action == "create_task":
        text = f"Task '{validated.title}' has been added"
        if validated.due is not None:
            text += f", due {validated.due.strftime('%A, %Y-%m-%d').strip()}"
        return text + "."
    if action == "update_task":
        return "The task has been updated successfully."
    if action == "complete_task":
        return "The task has been marked as completed."
    return "Done."


async def _verbalize_tool_result(messages, api_key: str, action: str, data: dict) -> list[str]:
    """Speak read-action results naturally via the existing streamer."""
    import json as _json

    spoken_messages = [
        {
            "role": "developer",
            "content": (
                "You are Raala, a professional receptionist. Summarize ONLY the "
                "Calendar tool result below in clear, natural spoken language using "
                "short conversational sentences. Never invent events, times, or "
                "availability. If the result is empty, say there is nothing scheduled. "
                "Do not use Markdown, bullet points, or symbols."
            ),
        },
        *[message for message in messages if message.get("role") == "user"][-3:],
        {
            "role": "developer",
            "content": f"Calendar tool '{action}' returned: {_json.dumps(data)[:4000]}",
        },
    ]
    events: list[str] = []
    async for event in _stream_muse_chat(spoken_messages, api_key):
        events.append(event)
    return events


def _selection_has_valid_args(action: str, arguments: dict) -> bool:
    """Check a reselected action carries usable new details."""
    from app.agent.action_models import Selection

    try:
        Selection(action=action, arguments=arguments).validated_args()
    except Exception:
        return False
    return True


_GOOGLE_LOGIN_URL = "http://127.0.0.1:8002/auth/google/login"


def _google_connect_message() -> str | None:
    """Spoken next step when Google is needed but no account is linked."""
    deps = _google_deps()
    if not deps.configured():
        return None
    from app.integrations.google import token_store

    if token_store.is_connected(deps.token_path):
        return None
    return (
        "Your Google account is not connected yet. Tap the Connect Google link "
        "on screen, approve access, then ask me again."
    )


def _looks_like_meeting_request(user_text: str) -> bool:
    """True for schedule/book-a-meeting intents (not pure reads)."""
    lowered = (user_text or "").lower()
    if not any(word in lowered for word in ("meeting", "appointment", "schedule", "book")):
        return False
    if any(word in lowered for word in ("schedul", "book", "set up", "set-up", "arrange", "plan")):
        return True
    return "meeting" in lowered and any(
        word in lowered for word in ("tomorrow", "today", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "morning", "afternoon", "evening", " at ", "am", "pm", "here to")
    )


def _fast_path_action(user_text: str) -> str | None:
    """Skip the selector LLM call for unambiguous intents.

    Returns a registered action with empty arguments (slot-filling and the
    executor validate the rest), or None to use the selector. Bare send-mail
    intents and "meetings today" reads never need the LLM to choose.
    Meeting scheduling itself is handled before the selector so one Google
    range read can render the 10 daily slots with no LLM call.
    """
    lowered = (user_text or "").lower()
    if (
        "meeting" in lowered
        and "today" in lowered
        and ("?" in lowered or lowered.startswith(
            ("what", "when", "list", "show", "do ", "how", "any", "are ", "my ")
        ))
    ):
        return "get_today_events"
    if "send" in lowered and ("mail" in lowered or "email" in lowered or "e-mail" in lowered):
        if "@" in lowered or "subject" in lowered or "message" in lowered or "body" in lowered:
            return None
        if any(ch.isdigit() for ch in lowered):
            return None
        return "send_email"
    return None


def _email_draft_store() -> EmailDraftStore:
    global _email_drafts
    if _email_drafts is None:
        _email_drafts = EmailDraftStore()
    return _email_drafts


def _meeting_draft_store() -> MeetingDraftStore:
    global _meeting_drafts
    if _meeting_drafts is None:
        _meeting_drafts = MeetingDraftStore()
    return _meeting_drafts


async def _tomorrow_free_slots() -> tuple[object, list, str, str]:
    """One Google range read for tomorrow 9 AM-7 PM; returns (day, free, date, tz)."""
    from datetime import datetime, timedelta

    from app.agent import meetings as meeting_core
    from app.agent.timezone import local_tz
    from app.integrations.google import calendar as calendar_api

    now = datetime.now(local_tz())
    day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = day.replace(hour=9)
    day_end = day.replace(hour=19)
    tz_label = meeting_core.friendly_tz_label(now.strftime("%Z"))
    deps = _google_deps()
    busy: list[tuple[object, object]] = []
    try:
        from app.integrations.google import token_store

        if deps.configured() and token_store.is_connected(deps.token_path):
            async with deps.client_factory() as client:
                events = await calendar_api.list_events_in_range(
                    client, deps.ensure_token, day_start, day_end, limit=50
                )
            for item in events:
                try:
                    b_start = datetime.fromisoformat(str(item.get("start", "")).replace("Z", "+00:00"))
                    b_end = datetime.fromisoformat(str(item.get("end", "")).replace("Z", "+00:00"))
                    busy.append((b_start, b_end))
                except (ValueError, TypeError):
                    continue
    except Exception:
        logger.warning("Meeting availability fell back to all slots.", exc_info=False)
    busy.extend(meeting_core.booked_intervals(BASE_DIR, day_start, day_end))
    free = meeting_core.free_slots(day, busy)
    return day, free, day.strftime("%Y-%m-%d"), tz_label


async def _meeting_request_events(user_text: str = "", chat_mode: bool = False) -> list[str]:
    """Immediate meeting flow: availability + brief info + slot cards + form.

    One Google range read, no selector LLM call. Emits spoken text plus
    `meeting_slots` and `meeting_form` action events so the visitor can tap
    or speak a time. In chat mode the form stays closed and details are
    collected with questions instead. When the request names an explicit
    time, it is verified first: a taken time gets a "not available" answer
    with alternatives and no form; a free time preselects the form. The
    booking itself is confirmed only after creation; mail and sheet writes
    run in the background afterwards.
    """
    from app.agent import meetings as meeting_core

    day, free, date_text, tz_label = await _tomorrow_free_slots()
    requested = None
    if meeting_core.explicit_time_mentioned(user_text):
        requested = meeting_core.match_slot(user_text, free)
        if requested is None:
            events: list[str] = []
            events.extend(_sse_text("Let me check the available times."))
            events.extend(
                _sse_text(
                    "That time is not available. "
                    + meeting_core.alternative_suggestion(free)
                )
            )
            events.append(
                _sse_event(
                    {
                        "type": "action",
                        "action": "meeting_slots",
                        "date": date_text,
                        "timezone": tz_label,
                        "slots": [
                            {
                                "start": start.isoformat(),
                                "end": end.isoformat(),
                                "label": meeting_core.slot_label(start),
                            }
                            for start, end in free
                        ],
                    }
                )
            )
            return events
    events = []
    events.extend(
        _sse_text(
            "Sure, I can help you schedule a meeting for tomorrow. "
            "Let me check the available times."
        )
    )
    events.extend(_sse_text(meeting_core.availability_speech(day, free, tz_label)))
    events.extend(_sse_text(meeting_core.meeting_intro_brief()))
    slots = [
        {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "label": meeting_core.slot_label(start),
        }
        for start, end in free
    ]
    events.append(
        _sse_event(
            {
                "type": "action",
                "action": "meeting_slots",
                "date": date_text,
                "timezone": tz_label,
                "slots": slots,
            }
        )
    )
    if chat_mode:
        # Chat mode collects details with questions; the form stays closed.
        return events
    if requested is not None:
        form_start, form_end = requested[0].isoformat(), requested[1].isoformat()
    elif slots:
        form_start, form_end = slots[0]["start"], slots[0]["end"]
    else:
        form_start, form_end = "", ""
    events.append(
        _sse_event(
            {
                "type": "action",
                "action": "meeting_form",
                "start": form_start,
                "end": form_end,
                "timezone": meeting_core.MEETING_TIMEZONE,
                "message": "I opened the meeting form. Please complete the remaining details.",
            }
        )
    )
    events.extend(
        _sse_text("I've opened a short form so I can collect the details needed for your meeting.")
    )
    return events


async def _meeting_availability_only_events() -> list[str]:
    """Live availability answer with slot cards only — never the form.

    Used for "what slots are free?" questions in both voice and chat mode.
    The visitor picks a time from here; the form opens only after a time is
    verified available.
    """
    from app.agent import meetings as meeting_core

    day, free, date_text, tz_label = await _tomorrow_free_slots()
    events: list[str] = []
    events.extend(_sse_text("Let me check the available times."))
    events.extend(_sse_text(meeting_core.availability_speech(day, free, tz_label)))
    if free:
        events.extend(
            _sse_text("Tap a time here, or just say it, and I will book it for you.")
        )
    slots = [
        {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "label": meeting_core.slot_label(start),
        }
        for start, end in free
    ]
    events.append(
        _sse_event(
            {
                "type": "action",
                "action": "meeting_slots",
                "date": date_text,
                "timezone": tz_label,
                "slots": slots,
            }
        )
    )
    return events


def _check_cancel(session_id: str, user_text: str) -> list[str] | None:
    """Drop an open proposal or draft on explicit cancel."""
    from app.agent.drafts import is_cancel_text

    if not session_id or not is_cancel_text(user_text):
        return None
    had = _confirmation_store().get_valid(session_id) is not None
    had = _email_draft_store().get_valid(session_id) is not None or had
    had = _meeting_draft_store().get_valid(session_id) is not None or had
    had = _valid_voice_booking(session_id) is not None or had
    _confirmation_store().discard(session_id)
    _email_draft_store().discard(session_id)
    _meeting_draft_store().discard(session_id)
    _voice_bookings.pop(session_id, None)
    _meeting_offered.pop(session_id, None)
    if had:
        return _sse_text("OK, I've dropped that. What would you like to do?")
    return None


async def _run_email_draft(registry, session_id: str, fields: dict) -> list[str]:
    """Validate merged email fields: propose, ask for the next slot, or report."""
    from app.agent.action_models import SendEmailArgs
    from app.agent.drafts import SLOT_QUESTIONS, next_missing_slot
    from app.agent.tool_executor import execute_tool

    drafts = _email_draft_store()
    try:
        validated = SendEmailArgs.model_validate(fields)
    except Exception:
        drafts.save(session_id, fields, next_missing_slot(fields))
        missing = next_missing_slot(fields) or "to"
        return _sse_text(SLOT_QUESTIONS[missing])
    result = await execute_tool(
        registry, "send_email", validated.model_dump(), {"session_id": session_id}
    )
    if result.needs_confirmation:
        drafts.discard(session_id)
        if not session_id:
            return _sse_text("Please reload the app and ask again so I can track your confirmation.")
        _confirmation_store().propose(session_id, "send_email", result.arguments or {}, result.proposal)
        return _sse_text(result.proposal)
    if result.ok:
        drafts.discard(session_id)
        return _sse_text(result.message)
    drafts.save(session_id, fields, next_missing_slot(fields))
    return _sse_text(result.message)


async def _handle_email_turn(
    registry, session_id: str, raw_args: dict, user_text: str, api_key: str
) -> list[str]:
    """Merge one turn into the email draft and advance it."""
    from app.agent.drafts import extract_email_slots

    fields: dict = {}
    if session_id:
        draft = _email_draft_store().get_valid(session_id)
        if draft is not None:
            fields.update(draft["fields"])
    fields.update(extract_email_slots(raw_args, user_text))
    if fields.get("body"):
        drafted_events = await _draft_email_body(
            registry,
            session_id,
            fields,
            str(fields["body"]),
            api_key,
            source_text=user_text,
        )
        if drafted_events is not None:
            return drafted_events
    return await _run_email_draft(registry, session_id, fields)


async def _draft_email_body(
    registry,
    session_id: str,
    fields: dict,
    purpose: str,
    api_key: str,
    *,
    source_text: str = "",
) -> list[str] | None:
    """AI-draft the message from the user's purpose; None means use raw words.

    Labelled dictation ("body: ...") stays verbatim. Any drafting failure also
    falls back to the raw words, so sending never depends on the drafter.
    """
    import re

    label_text = source_text or purpose or ""
    if re.search(
        r"\b(?:message|body)\s*(?:would be|is|should say|say|:)",
        label_text,
        re.IGNORECASE,
    ):
        return None
    try:
        from app.agent.mail_drafter import draft_email

        drafted = await draft_email(
            lambda payload: _selector_post(payload, api_key),
            purpose=purpose,
            subject=fields.get("subject", "") or "",
            to=fields.get("to") or [],
        )
    except Exception as exc:
        logger.warning(
            "Email drafting failed (%s); using the user's words.",
            type(exc).__name__,
        )
        return None
    fields = dict(fields)
    fields["body"] = drafted["body"]
    if not fields.get("subject") and drafted.get("subject"):
        fields["subject"] = drafted["subject"]
    return await _run_email_draft(registry, session_id, fields)


async def _continue_email_draft(registry, session_id: str, draft: dict, user_text: str, api_key: str) -> list[str] | None:
    """Fold a free-text turn into the open draft; None means normal chat."""
    from app.agent.drafts import extract_email_slots, fill_awaiting_slot

    fields = dict(draft["fields"])
    body_before = fields.get("body")
    draft_attempted = False
    if draft.get("awaiting") == "body":
        draft_attempted = True
        drafted_events = await _draft_email_body(
            registry,
            session_id,
            fields,
            user_text,
            api_key,
            source_text=user_text,
        )
        if drafted_events is not None:
            return drafted_events
    if draft.get("awaiting") == "subject":
        from app.agent.drafts import split_subject_message

        split = split_subject_message(user_text)
        if split is not None:
            fields.update(split)
            drafted_events = await _draft_email_body(
                registry,
                session_id,
                fields,
                str(split["body"]),
                api_key,
                source_text=user_text,
            )
            if drafted_events is not None:
                return drafted_events
            return await _run_email_draft(registry, session_id, fields)
    slots = extract_email_slots({}, user_text)
    if not slots:
        filled = fill_awaiting_slot(draft.get("awaiting"), user_text)
        if filled is None:
            return None
        key, value = filled
        fields[key] = value
    else:
        fields.update(slots)
    if fields.get("body") and fields.get("body") != body_before and not draft_attempted:
        drafted_events = await _draft_email_body(
            registry,
            session_id,
            fields,
            str(fields["body"]),
            api_key,
            source_text=user_text,
        )
        if drafted_events is not None:
            return drafted_events
    return await _run_email_draft(registry, session_id, fields)


async def _run_meeting_draft(registry, session_id: str, fields: dict) -> list[str]:
    """Validate merged meeting fields: propose, ask for the next slot, or report."""
    from datetime import datetime

    from app.agent.action_models import CreateEventArgs
    from app.agent.drafts import MEETING_QUESTIONS, next_missing_meeting_slot
    from app.agent.tool_executor import execute_tool

    drafts = _meeting_draft_store()
    iso = {
        key: (value.isoformat() if isinstance(value, datetime) else value)
        for key, value in fields.items()
    }
    try:
        validated = CreateEventArgs.model_validate(iso)
    except Exception:
        drafts.save(session_id, iso, next_missing_meeting_slot(iso))
        missing = next_missing_meeting_slot(iso) or "summary"
        return _sse_text(MEETING_QUESTIONS[missing])
    if validated.end <= validated.start:
        drafts.save(session_id, iso, "end")
        return _sse_text(
            "The end time needs to be after the start "
            f"({validated.start.strftime('%A at %H:%M').strip()}). {MEETING_QUESTIONS['end']}"
        )
    result = await execute_tool(
        registry, "create_calendar_event", validated.model_dump(mode="json"), {"session_id": session_id}
    )
    if result.needs_confirmation:
        drafts.discard(session_id)
        if not session_id:
            return _sse_text("Please reload the app and ask again so I can track your confirmation.")
        _confirmation_store().propose(session_id, "create_calendar_event", result.arguments or {}, result.proposal)
        return _sse_text(result.proposal)
    if result.ok:
        drafts.discard(session_id)
        return _sse_text(result.message)
    drafts.save(session_id, iso, next_missing_meeting_slot(iso))
    return _sse_text(result.message)


async def _handle_meeting_turn(registry, session_id: str, raw_args: dict, user_text: str) -> list[str]:
    """Merge one turn into the meeting draft and advance it."""
    from app.agent.drafts import merge_meeting_fields

    fields: dict = {}
    if session_id:
        draft = _meeting_draft_store().get_valid(session_id)
        if draft is not None:
            fields.update(draft["fields"])
    fields, _ = merge_meeting_fields(fields, raw_args or {}, user_text)
    return await _run_meeting_draft(registry, session_id, fields)


async def _continue_meeting_draft(registry, session_id: str, mdraft: dict, user_text: str) -> list[str] | None:
    """Fold a free-text turn into the open meeting draft; None means normal chat."""
    from app.agent.drafts import merge_meeting_fields

    fields = dict(mdraft["fields"])
    merged, progressed = merge_meeting_fields(fields, {}, user_text)
    if not progressed:
        return None
    return await _run_meeting_draft(registry, session_id, merged)


def _looks_like_slot_choice(user_text: str) -> bool:
    """True for a clicked/typed slot pick ("Book the 10:00 AM slot ...")."""
    return "slot" in (user_text or "").lower()


def _looks_like_bare_time(user_text: str) -> bool:
    """True for a time-only answer ("10", "10 AM", "ten" excluded)."""
    import re as _re

    text = (user_text or "").strip().lower()
    if not text or len(text) > 24 or "@" in text:
        return False
    return bool(_re.search(r"\d", text))


def _looks_like_availability_request(user_text: str) -> bool:
    """True for "what slots are free?" style questions (no booking intent).

    Slot picks ("book the 10 AM slot") are handled earlier, so any booking
    verb here means the full scheduling flow, not a bare availability check.
    """
    lowered = (user_text or "").strip().lower()
    if any(
        word in lowered
        for word in ("schedul", "book", "set up", "set-up", "arrange", "reschedul", "cancel")
    ):
        return False
    has_cue = any(
        word in lowered
        for word in ("available", "availability", "free", "slots", "slot", "openings", "open times")
    )
    if not has_cue:
        return False
    return any(
        word in lowered
        for word in (
            "tomorrow", "today", "tonight", "morning", "afternoon", "evening",
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
            "sunday", "meeting", "appointment", "slot", "calendar",
        )
    )


def _looks_like_booking_status(user_text: str) -> bool:
    """True for "did you schedule/book it?" style status checks.

    The question words must lead the turn so detail answers like "I have a
    meeting about pricing" are never mistaken for a status check.
    """
    lowered = (user_text or "").strip().lower()
    asked = lowered.startswith(
        ("did ", "did you", "have you", "has ", "is it", "is the", "what", "status", "confirm")
    )
    about = any(word in lowered for word in ("schedul", "book", "meeting", "appointment"))
    return asked and about


def _valid_voice_booking(session_id: str) -> dict | None:
    """Return the live voice booking for a session, if any."""
    import time as _time

    booking = _voice_bookings.get(session_id)
    if booking is None:
        return None
    if _time.time() - booking.get("updated_at", 0) > VOICE_BOOKING_TTL_SECONDS:
        _voice_bookings.pop(session_id, None)
        return None
    return booking


def _touch_voice_booking(session_id: str) -> None:
    import time as _time

    if session_id in _voice_bookings:
        _voice_bookings[session_id]["updated_at"] = _time.time()


async def _start_voice_booking(
    session_id: str, user_text: str, chat_mode: bool = False
) -> list[str] | None:
    """Begin/refresh voice slot-filling from a slot pick or bare-time answer.

    Always returns speech events: a short ack plus the next missing question,
    the confirm summary, or alternatives when the time is murky. In chat mode
    the form stays closed and details are collected with questions instead.
    """
    import time as _time

    from app.agent import meetings as meeting_core

    if not _google_connected():
        events = _sse_text(
            "Your Google account is not connected yet. "
            "Please connect Google, then ask me to book it again."
        )
        events.append(
            _sse_event(
                {
                    "type": "action",
                    "action": "google_login",
                    "url": _GOOGLE_LOGIN_URL,
                    "label": "Connect Google",
                }
            )
        )
        return events
    day, free, _date_text, tz_label = await _tomorrow_free_slots()
    if not free:
        return _sse_text(
            "Tomorrow is fully booked. I can check the next available date if you like."
        )
    slot = meeting_core.match_slot(user_text, free)
    if slot is None:
        if meeting_core.explicit_time_mentioned(user_text):
            return _sse_text(
                "That time is not available. "
                + meeting_core.alternative_suggestion(free)
            )
        return _sse_text(
            "I did not catch which time you meant. "
            + meeting_core.alternative_suggestion(free)
        )
    start, end = slot
    previous = _voice_bookings.get(session_id) or {}
    details: dict = {
        "topic": previous.get("topic", ""),
        "meeting_type": previous.get("meeting_type", ""),
        "name": previous.get("name", ""),
        "email": previous.get("email", ""),
        "email_auto": previous.get("email_auto", False),
        "ask_email": previous.get("ask_email", False),
    }
    found_email = meeting_core.extract_email(user_text)
    if found_email:
        details["email"] = found_email
        details["email_auto"] = False
    found_type = meeting_core.parse_meeting_type(user_text)
    if found_type:
        details["meeting_type"] = found_type
    _voice_bookings[session_id] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timezone": tz_label,
        "stage": "collecting",
        "updated_at": _time.time(),
        **details,
    }
    _meeting_offered.pop(session_id, None)
    missing = meeting_core.next_missing_voice_field(details)
    if missing == "email" and not details["email_auto"] and not details["ask_email"]:
        details["email"] = meeting_core.TESTING_EMAIL
        details["email_auto"] = True
        _voice_bookings[session_id].update(details)
        missing = None
    form_action = _sse_event(
        {
            "type": "action",
            "action": "meeting_form",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timezone": meeting_core.MEETING_TIMEZONE,
            "message": "I opened the meeting form. Please complete the remaining details.",
            "prefill": {
                "topic": details["topic"],
                "meeting_type": details["meeting_type"],
                "name": details["name"],
                "email": details["email"],
            },
        }
    )
    if missing is None:
        _voice_bookings[session_id]["stage"] = "confirm"
        summary = meeting_core.confirm_summary(details, day, start, tz_label)
        lead = ""
        if details["email_auto"]:
            lead = f"I'll use {meeting_core.TESTING_EMAIL} for this test. "
        return _sse_text(lead + summary)
    if chat_mode:
        # Chat mode: no form; ask for the next missing detail in the thread.
        events = _sse_text(
            f"Got it: {meeting_core.spoken_date(day)} at "
            f"{meeting_core.spoken_time(start)} {tz_label}."
        )
        events.extend(_sse_text(meeting_core.VOICE_QUESTIONS[missing]))
        return events
    # Form-first: the form is the default input. Voice only harvests what was
    # said, mirrors it into the form, and names what is still missing there.
    still_missing = [
        meeting_core.VOICE_FIELD_LABELS[key]
        for key in meeting_core.VOICE_FIELD_ORDER
        if not str(details.get(key) or "").strip()
    ]
    events = _sse_text(
        f"Got it: {meeting_core.spoken_date(day)} at "
        f"{meeting_core.spoken_time(start)} {tz_label}. "
        f"I've put that in the form. Please add {', '.join(still_missing)} "
        "there and press Book meeting."
    )
    events.append(form_action)
    return events


async def _continue_voice_booking(
    session_id: str, voice: dict, user_text: str, chat_mode: bool = False
) -> list[str]:
    """Fold one answer into the in-progress voice booking; one question max.

    In chat mode the form stays closed and the next missing detail is asked
    in the thread instead of pointing at the form.
    """
    import time as _time

    from app.agent import meetings as meeting_core
    from app.agent.confirmation import is_affirmative_confirmation

    _touch_voice_booking(session_id)
    day, free, _date_text, tz_label = await _tomorrow_free_slots()
    start = end = None
    try:
        from datetime import datetime as _dt

        start = _dt.fromisoformat(str(voice.get("start", "")))
        end = _dt.fromisoformat(str(voice.get("end", "")))
    except (ValueError, TypeError):
        start = end = None

    def _still_free() -> bool:
        if start is None or end is None:
            return False
        return any(s.isoformat() == start.isoformat() and e.isoformat() == end.isoformat() for s, e in free)

    lowered = (user_text or "").strip().lower()
    heard_before = (
        voice.get("topic", ""),
        voice.get("meeting_type", ""),
        voice.get("name", ""),
        voice.get("email", ""),
        voice.get("start", ""),
    )
    if _looks_like_booking_status(user_text) and not (
        voice.get("stage") == "confirm" and is_affirmative_confirmation(user_text)
    ):
        # "Did you schedule the meeting?" mid-flow: report state, never restart.
        if voice.get("stage") == "confirm":
            try:
                from datetime import datetime as _dt

                _sdt = _dt.fromisoformat(str(voice.get("start", "")))
            except (ValueError, TypeError):
                _sdt = day
            return _sse_text(
                "Not booked yet. " + meeting_core.confirm_summary(voice, day, _sdt, tz_label)
            )
        _next = meeting_core.next_missing_voice_field(voice) or "topic"
        return _sse_text(
            "Not booked yet. " + meeting_core.VOICE_QUESTIONS[_next]
        )
    if voice.get("stage") == "confirm":
        if is_affirmative_confirmation(user_text) or lowered in ("confirm", "confirmed", "book it", "go ahead"):
            if not _still_free():
                _voice_bookings.pop(session_id, None)
                return _sse_text(
                    "That time was just booked by someone else. "
                    + meeting_core.alternative_suggestion(free)
                )
            fields = {
                "name": voice.get("name", ""),
                "email": voice.get("email", ""),
                "phone": "",
                "topic": voice.get("topic", ""),
                "meeting_type": voice.get("meeting_type", ""),
                "notes": "",
                "timezone": voice.get("timezone", tz_label),
                "start": voice.get("start", ""),
                "end": voice.get("end", ""),
            }
            try:
                booking = await _create_meeting_booking(fields)
            except MeetingBookingError as exc:
                if exc.code == "taken":
                    _voice_bookings.pop(session_id, None)
                    _day2, free2, _, _ = await _tomorrow_free_slots()
                    return _sse_text(
                        "That time was just booked by someone else. "
                        + meeting_core.alternative_suggestion(free2)
                    )
                if exc.code == "not_connected":
                    events = _sse_text(str(exc))
                    events.append(
                        _sse_event(
                            {
                                "type": "action",
                                "action": "google_login",
                                "url": _GOOGLE_LOGIN_URL,
                                "label": "Connect Google",
                            }
                        )
                    )
                    return events
                return _sse_text(f"I could not book it: {exc}. Please try again.")
            _voice_bookings.pop(session_id, None)
            _last_bookings[session_id] = booking
            events = _sse_text(
                meeting_core.confirmation_speech(booking)
                + " Setting that up now: you will get confirmation once it is ready."
            )
            events.append(
                _sse_event(
                    {
                        "type": "action",
                        "action": "meeting_confirmation",
                        "message": meeting_core.confirmation_speech(booking),
                        "booking": {
                            "booking_id": booking.get("booking_id", ""),
                            "meeting_date": booking.get("meeting_date", ""),
                            "meeting_time": booking.get("meeting_time", ""),
                        },
                    }
                )
            )
            return events
        if lowered in ("no", "nope", "don't", "dont", "change", "wait"):
            voice["stage"] = "collecting"
            _touch_voice_booking(session_id)
            return _sse_text("No problem. What should I change: the time, topic, type, name, or email?")

    # Collecting stage (or a correction to a summary): harvest what is new.
    # A bare time ("2 PM", "at 10") always means the slot; anything longer
    # needs the word "slot" so names like "John 3rd" never retime by accident.
    import re as _re

    _bare = (user_text or "").strip().lower()
    _bare = _re.sub(r"^at\s+", "", _bare)
    _is_time_only = bool(_re.fullmatch(r"\d{1,2}(?::\d{2})?\s*(a\.?m\.?|p\.?m\.?)?", _bare))
    retimed = meeting_core.match_slot(user_text, free)
    if retimed is not None and (_looks_like_slot_choice(user_text) or _is_time_only):
        new_start, new_end = retimed
        voice["start"] = new_start.isoformat()
        voice["end"] = new_end.isoformat()
        voice["stage"] = "collecting"
        _touch_voice_booking(session_id)
        start, end = new_start, new_end
    else:
        found_email = meeting_core.extract_email(user_text)
        if found_email:
            voice["email"] = found_email
            voice["email_auto"] = False
            voice["ask_email"] = False
        found_type = meeting_core.parse_meeting_type(user_text)
        if found_type:
            voice["meeting_type"] = found_type
        # A named time that matches nothing free is taken (or unknown): say
        # so plainly instead of storing it as a topic and touching the form.
        # Plain counts ("2 people") never reach here: explicit_time_mentioned
        # requires clock phrasing, and the turn must read as a time request.
        if (
            retimed is None
            and not found_email
            and not found_type
            and meeting_core.explicit_time_mentioned(user_text)
            and (_is_time_only or "book" in lowered or "slot" in lowered)
        ):
            _touch_voice_booking(session_id)
            return _sse_text(
                "That time is not available. "
                + meeting_core.alternative_suggestion(free)
            )
        # "I want to fill the form" with nothing else new: restore the form
        # via the tail action and ask the next missing field, without
        # storing the sentence itself as an answer.
        _wants_form_only = ("form" in lowered or "fill" in lowered) and not (
            found_email or found_type or retimed
        )
        if not found_email and not found_type and not retimed and not _wants_form_only:
            missing_now = meeting_core.next_missing_voice_field(voice)
            if missing_now is None:
                # Post-"no" correction naming a field: clear it, then ask again.
                if "change" in lowered or "correct" in lowered:
                    return _sse_text(
                        "Of course. What should I change: the time, topic, type, name, or email?"
                    )
                if "topic" in lowered:
                    voice["topic"] = ""
                elif "email" in lowered or "mail" in lowered:
                    voice["email"] = ""
                    voice["ask_email"] = True
                elif "name" in lowered:
                    voice["name"] = ""
                elif "time" in lowered or "slot" in lowered or "hour" in lowered:
                    voice["stage"] = "collecting"
                    return _sse_text(
                        "Which time should I use instead? "
                        + meeting_core.alternative_suggestion(free)
                    )
                elif "type" in lowered or "video" in lowered or "phone" in lowered:
                    voice["meeting_type"] = ""
            elif missing_now in ("topic", "name"):
                cleaned = _re.sub(
                    r"^(my name is|i am|i'm|this is|it is|it's|the topic is|about|regarding)\s+",
                    "",
                    (user_text or "").strip(),
                    flags=_re.IGNORECASE,
                )
                voice[missing_now] = meeting_core.sanitize_text(cleaned, 200 if missing_now == "topic" else 120)
                if missing_now == "name":
                    voice["name"] = voice["name"].rstrip(".,;!?").strip()
    _touch_voice_booking(session_id)
    missing = meeting_core.next_missing_voice_field(voice)
    just_filled_test_email = False
    if missing == "email" and not voice.get("email_auto") and not voice.get("ask_email"):
        voice["email"] = meeting_core.TESTING_EMAIL
        voice["email_auto"] = True
        just_filled_test_email = True
        _touch_voice_booking(session_id)
        missing = None
    try:
        from datetime import datetime as _dt

        _slot_start = _dt.fromisoformat(str(voice.get("start", "")))
        _slot_end = _dt.fromisoformat(str(voice.get("end", "")))
    except (ValueError, TypeError):
        _slot_start = _slot_end = day
    form_action = _sse_event(
        {
            "type": "action",
            "action": "meeting_form",
            "start": _slot_start.isoformat(),
            "end": _slot_end.isoformat(),
            "timezone": meeting_core.MEETING_TIMEZONE,
            "message": "I opened the meeting form. Please complete the remaining details.",
            "prefill": {
                "topic": voice.get("topic", ""),
                "meeting_type": voice.get("meeting_type", ""),
                "name": voice.get("name", ""),
                "email": voice.get("email", ""),
            },
        }
    )
    if missing is None:
        voice["stage"] = "confirm"
        _touch_voice_booking(session_id)
        summary = meeting_core.confirm_summary(voice, day, _slot_start, tz_label)
        lead = ""
        if just_filled_test_email:
            lead = f"I'll use {meeting_core.TESTING_EMAIL} for this test. "
        return _sse_text(lead + summary)
    if chat_mode:
        # Chat mode: no form; ask for the next missing detail in the thread.
        return _sse_text(meeting_core.VOICE_QUESTIONS[missing])
    # Form-first: mirror what was heard into the form and name what is left.
    heard_now = (
        voice.get("topic", ""),
        voice.get("meeting_type", ""),
        voice.get("name", ""),
        voice.get("email", ""),
        voice.get("start", ""),
    )
    still_missing = [
        meeting_core.VOICE_FIELD_LABELS[key]
        for key in meeting_core.VOICE_FIELD_ORDER
        if not str(voice.get(key) or "").strip()
    ]
    if heard_now == heard_before:
        events = _sse_text(
            f"The form is open with your time. Please add {', '.join(still_missing)} "
            "there and press Book meeting."
        )
    else:
        events = _sse_text(
            f"Added. I've updated the form — it still needs {', '.join(still_missing)}. "
            "Press Book meeting when it is complete."
        )
    events.append(form_action)
    return events


async def _maybe_handle_calendar_tool(request, messages, api_key, spans=None) -> AsyncIterator[str]:
    """Yield SSE events for tool turns; yield nothing to use normal chat.

    When `spans` is given, stage timings (selector_ms, tool_ms, verbalize_ms,
    exec_ms) are recorded into it for latency measurement.
    """
    import time

    from app.agent import selector as action_selector
    from app.agent.confirmation import (
        is_affirmative_confirmation,
        is_explicit_confirmation,
        matching_details_for,
    )
    from app.agent.tool_executor import execute_tool
    from app.agent.timezone import today_label

    timings = spans if spans is not None else {}
    user_text = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            user_text = str(message.get("content", ""))
            break
    session_id = (request.session_id or "").strip()
    chat_mode = bool(getattr(request, "chat_mode", False))
    store = _confirmation_store()
    email_drafts = _email_draft_store()
    cancelled = _check_cancel(session_id, user_text)
    if cancelled is not None:
        for event in cancelled:
            yield event
        return

    if session_id:
        pending = store.get_valid(session_id)
        if pending is not None and (
            is_explicit_confirmation(user_text) or is_affirmative_confirmation(user_text)
        ):
            if action_selector.looks_tool_related(user_text):
                registry = _agent_registry()
                sel_start = time.perf_counter()
                selection = await action_selector.select_action(
                    lambda payload: _selector_post(payload, api_key),
                    messages,
                    today_label(),
                )
                timings["selector_ms"] = round((time.perf_counter() - sel_start) * 1000)
                sel_action = selection.get("action") or "none"
                sel_usable = sel_action in registry.names() and _selection_has_valid_args(
                    sel_action, selection.get("arguments") or {}
                )
                if sel_action == "none" or not sel_usable or (
                    sel_action == pending.action
                    and matching_details_for(
                        pending.action, pending.arguments, selection.get("arguments") or {}
                    )
                ):
                    confirmed = store.consume(session_id)
                    if confirmed is None:
                        for event in _sse_text("That confirmation has expired. Please ask again."):
                            yield event
                        return
                    async for event in _execute_confirmed(confirmed, timings):
                        yield event
                    return
                store.discard(session_id)
            else:
                confirmed = store.consume(session_id)
                if confirmed is None:
                    for event in _sse_text("That confirmation has expired. Please ask again."):
                        yield event
                    return
                async for event in _execute_confirmed(confirmed, timings):
                    yield event
                return

    # Voice slot-filling: an in-progress booking answers one question at a time.
    if session_id:
        voice = _valid_voice_booking(session_id)
        if voice is not None and not (
            _looks_like_meeting_request(user_text) and not _looks_like_slot_choice(user_text)
        ):
            for event in await _continue_voice_booking(session_id, voice, user_text, chat_mode):
                yield event
            return
        if voice is not None:
            _voice_bookings.pop(session_id, None)
    # "Did you schedule the meeting?" answers from the last booking result.
    if session_id and _looks_like_booking_status(user_text) and session_id in _last_bookings:
        from app.agent import meetings as _meeting_core

        booking = _last_bookings[session_id]
        try:
            from datetime import datetime as _dt

            _start = _dt.fromisoformat(str(booking.get("start", "")).replace("Z", "+00:00"))
            when = f"{_meeting_core.spoken_date(_start)} at {_meeting_core.spoken_time(_start)}"
        except (ValueError, TypeError):
            when = f"{booking.get('meeting_date', '')} at {booking.get('meeting_time', '')}"
        for event in _sse_text(
            f"Yes, it is booked: {booking.get('topic', 'your meeting')} on {when} "
            f"{booking.get('timezone', '')} under {booking.get('name', '')}. "
            f"Booking ID {booking.get('booking_id', '')}."
        ):
            yield event
        return
    # Slot pick (clicked card or spoken time) starts voice collection, not a
    # repeat of the availability spiel. A bare "slots" question with no time
    # and no booking verb falls through to the availability-only answer.
    if session_id and (
        (
            _looks_like_slot_choice(user_text)
            and (
                "book" in (user_text or "").lower()
                or _looks_like_bare_time(user_text)
            )
        )
        or (
            _looks_like_bare_time(user_text)
            and _meeting_offered.get(session_id, 0) > time.time() - VOICE_BOOKING_TTL_SECONDS
        )
    ):
        for event in await _start_voice_booking(session_id, user_text, chat_mode):
            yield event
        return
    # Bare availability question: live slots, cards only, never the form.
    if _looks_like_availability_request(user_text):
        timings["selector_ms"] = 0
        for event in await _meeting_availability_only_events():
            yield event
        if session_id:
            _meeting_offered[session_id] = time.time()
        return
    # Meeting-only fast flow: no selector LLM call, one Google range read.
    if _looks_like_meeting_request(user_text):
        timings["selector_ms"] = 0
        for event in await _meeting_request_events(user_text, chat_mode):
            yield event
        if session_id:
            _meeting_offered[session_id] = time.time()
        return
    draft = email_drafts.get_valid(session_id) if session_id else None
    mdraft = _meeting_draft_store().get_valid(session_id) if session_id else None
    if draft is None and mdraft is None and not action_selector.looks_tool_related(user_text):
        return
    # Fail fast while unconnected: surface the login URL on the first
    # tool-related turn, before spending a selection call or slot-filling.
    connect_msg = _google_connect_message()
    if connect_msg is not None:
        for event in _sse_text(connect_msg):
            yield event
        yield _sse_event(
            {
                "type": "action",
                "action": "google_login",
                "url": _GOOGLE_LOGIN_URL,
                "label": "Connect Google",
            }
        )
        return
    registry = _agent_registry()
    fast_action = _fast_path_action(user_text)
    if fast_action is not None:
        selection = {"action": fast_action, "arguments": {}}
        timings["selector_ms"] = 0
    else:
        sel_start = time.perf_counter()
        selection = await action_selector.select_action(
            lambda payload: _selector_post(payload, api_key),
            messages,
            today_label(),
        )
        timings["selector_ms"] = round((time.perf_counter() - sel_start) * 1000)
    action = selection.get("action") or "none"
    if action == "none":
        if draft is not None:
            draft_events = await _continue_email_draft(registry, session_id, draft, user_text, api_key)
            if draft_events is None:
                return
            for event in draft_events:
                yield event
            return
        if mdraft is not None:
            mdraft_events = await _continue_meeting_draft(registry, session_id, mdraft, user_text)
            if mdraft_events is None:
                return
            for event in mdraft_events:
                yield event
            return
    if action == "none":
        return
    if draft is not None and _selection_has_valid_args(action, selection.get("arguments") or {}):
        email_drafts.discard(session_id)
    if mdraft is not None and _selection_has_valid_args(action, selection.get("arguments") or {}):
        _meeting_draft_store().discard(session_id)
    if action not in registry.names():
        for event in _sse_text(
            "I schedule meetings. Tell me the day and time you want, pick an "
            "available slot, and I will book it for you."
        ):
            yield event
        return
    if action == "send_email":
        for event in await _handle_email_turn(
            registry, session_id, selection.get("arguments") or {}, user_text, api_key
        ):
            yield event
        return
    if action == "create_calendar_event":
        for event in await _handle_meeting_turn(
            registry, session_id, selection.get("arguments") or {}, user_text
        ):
            yield event
        return
    tool_start = time.perf_counter()
    result = await execute_tool(registry, action, selection.get("arguments") or {}, {"session_id": session_id})
    timings["tool_ms"] = round((time.perf_counter() - tool_start) * 1000)
    if result.needs_confirmation:
        if not session_id:
            for event in _sse_text("Please reload the app and ask again so I can track your confirmation."):
                yield event
            return
        store.propose(session_id, action, result.arguments or {}, result.proposal)
        for event in _sse_text(result.proposal):
            yield event
        return
    if not result.ok:
        for event in _sse_text(result.message):
            yield event
        return
    if action == "create_calendar_event":
        for event in _sse_text("Your meeting has been scheduled successfully."):
            yield event
        return
    if action == "send_email":
        for event in _sse_text(result.message):
            yield event
        return
    verb_start = time.perf_counter()
    verbalized = await _verbalize_tool_result(messages, api_key, action, result.data or {})
    timings["verbalize_ms"] = round((time.perf_counter() - verb_start) * 1000)
    for event in verbalized:
        yield event
    return


async def _chat_reply(messages, api_key: str, request):
    """Normal streaming chat, with the Calendar tool gate in front of it."""
    saw_tool_events = False
    spans: dict = {}
    async for event in _maybe_handle_calendar_tool(request, messages, api_key, spans):
        saw_tool_events = True
        yield event
    if saw_tool_events:
        yield _sse_event({"type": "done", "server_ms": spans})
        return
    async for event in _stream_muse_chat(messages, api_key):
        yield event


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    """Stream a conversational Muse Spark response as server-sent events."""
    api_key = os.getenv("MODEL_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="Muse Spark is not configured on the server.")
    if not CHAT_MODEL:
        raise HTTPException(status_code=503, detail="Muse Spark model is not configured on the server.")

    total_characters = sum(len(message.content) for message in request.messages)
    if total_characters > CHAT_MAX_TOTAL_CHARACTERS:
        raise HTTPException(
            status_code=413,
            detail=f"Conversation context cannot exceed {CHAT_MAX_TOTAL_CHARACTERS} characters.",
        )

    messages = [
        {
            "role": "developer",
            "content": (
                "You are Raala, a meeting scheduler. Answer in clear, "
                "natural spoken language using short conversational paragraphs. "
                "Do not use Markdown, bullet points, numbered lists, or decorative "
                "symbols; say list items as connected sentences. Keep the answer "
                "concise unless the user asks for detail. You schedule meetings: "
                "ask for the day and time, offer only available slots with the "
                "exact date and timezone, and confirm only after booking."
            ),
        },
        *[message.model_dump() for message in request.messages],
    ]
    return StreamingResponse(
        _chat_reply(messages, api_key, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class MeetingBookingRequest(BaseModel):
    """Meeting-form submission; the form is the confirmation (no extra step)."""

    session_id: str | None = None
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=320)
    phone: str = Field(default="", max_length=40)
    topic: str = Field(min_length=1, max_length=200)
    meeting_type: str = Field(min_length=1, max_length=30)
    notes: str = Field(default="", max_length=1000)
    timezone: str = Field(default="", max_length=80)
    start: str = Field(min_length=1, max_length=60)
    end: str = Field(min_length=1, max_length=60)


async def _send_meeting_confirmation_mail(booking: dict) -> None:
    """Background HTML confirmation; failures stay server-side."""
    try:
        from app.agent import meetings as meeting_core
        from app.integrations.google import gmail as gmail_api

        deps = _google_deps()
        from app.integrations.google import token_store

        if not deps.configured() or not token_store.is_connected(deps.token_path):
            return
        async with deps.client_factory() as client:
            await gmail_api.send_email_html_message(
                client,
                deps.ensure_token,
                [booking["email"]],
                f"Meeting confirmed for {booking['meeting_date']}",
                meeting_core.confirmation_text(booking),
                meeting_core.confirmation_html(booking),
            )
    except Exception:
        logger.warning("Meeting confirmation mail failed id=%s.", booking.get("booking_id", ""), exc_info=False)


@app.get("/api/meetings/availability")
async def meeting_availability(date: str | None = None) -> dict:
    """Return the 10 hourly slots for a date with live free/busy flags."""
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    from app.agent import meetings as meeting_core
    from app.agent.timezone import local_tz
    from app.integrations.google import calendar as calendar_api

    now = _dt.now(local_tz())
    if date:
        try:
            day = _dt.strptime(date.strip(), "%Y-%m-%d").replace(tzinfo=local_tz())
        except ValueError:
            raise HTTPException(status_code=422, detail="Use a date like 2026-11-05.")
    else:
        day = (now + _td(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = day.replace(hour=9)
    day_end = day.replace(hour=19)
    tz_label = meeting_core.friendly_tz_label(now.strftime("%Z"))
    deps = _google_deps()
    busy: list[tuple[object, object]] = []
    try:
        from app.integrations.google import token_store

        if deps.configured() and token_store.is_connected(deps.token_path):
            async with deps.client_factory() as client:
                events = await calendar_api.list_events_in_range(
                    client, deps.ensure_token, day_start, day_end, limit=50
                )
            for item in events:
                try:
                    b_start = _dt.fromisoformat(str(item.get("start", "")).replace("Z", "+00:00"))
                    b_end = _dt.fromisoformat(str(item.get("end", "")).replace("Z", "+00:00"))
                    busy.append((b_start, b_end))
                except (ValueError, TypeError):
                    continue
    except Exception:
        logger.warning("Availability lookup degraded; showing all slots.", exc_info=False)
    busy.extend(meeting_core.booked_intervals(BASE_DIR, day_start, day_end))
    free = meeting_core.free_slots(day, busy)
    free_keys = {(s.isoformat(), e.isoformat()) for s, e in free}
    slots = [
        {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "label": meeting_core.slot_label(start),
            "available": (start.isoformat(), end.isoformat()) in free_keys,
        }
        for start, end in meeting_core.day_slot_bounds(day)
    ]
    return {"date": day.strftime("%Y-%m-%d"), "timezone": tz_label, "slots": slots}


class MeetingBookingError(RuntimeError):
    """Booking failure with a user-safe message; code is taken|unavailable|failed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def _create_meeting_booking(fields: dict) -> dict:
    """Shared booking core for the form endpoint and the voice flow.

    Rechecks the live slot (double-booking guard), creates the Calendar event
    only when still free, records the booking, and fires mail + sheet writes
    in the background. Raises MeetingBookingError("taken"|"unavailable"|"failed").
    """
    from datetime import datetime as _dt

    from app.agent import meetings as meeting_core
    from app.integrations.google import calendar as calendar_api
    from app.integrations.google import sheets as sheets_api

    start = _dt.fromisoformat(fields["start"])
    end = _dt.fromisoformat(fields["end"])
    deps = _google_deps()
    connected = _google_connected()
    if not connected:
        raise MeetingBookingError(
            "not_connected",
            "Your Google account is not connected yet. "
            "Please connect Google, then ask me to book it again.",
        )
    if connected:
        try:
            async with deps.client_factory() as client:
                availability = await calendar_api.check_availability(
                    client, deps.ensure_token, start, end
                )
            if not availability.get("free", True):
                raise MeetingBookingError("taken", "That time was just booked.")
        except MeetingBookingError:
            raise
        except Exception as exc:
            logger.warning("Availability recheck failed (%s).", type(exc).__name__)
            raise MeetingBookingError("unavailable", "I could not verify the slot.") from exc
    booking_id = meeting_core.new_booking_id()
    created: dict = {}
    if connected:
        try:
            async with deps.client_factory() as client:
                created = await calendar_api.create_event(
                    client,
                    deps.ensure_token,
                    fields["topic"],
                    start,
                    end,
                    f"Booked by {fields['name']} ({fields['email']}). {fields['notes']}".strip(),
                    fields["timezone"],
                )
        except Exception as exc:
            logger.warning("Calendar create failed id=%s (%s).", booking_id, type(exc).__name__)
            raise MeetingBookingError("failed", "I could not create the meeting.") from exc
    booking = {
        "booking_id": booking_id,
        "created_at": _dt.now().astimezone().isoformat(),
        "meeting_date": start.strftime("%Y-%m-%d"),
        "meeting_time": meeting_core.slot_label(start),
        "start": fields["start"],
        "end": fields["end"],
        "timezone": fields["timezone"],
        "name": fields["name"],
        "email": fields["email"],
        "phone": fields["phone"],
        "topic": fields["topic"],
        "meeting_type": fields["meeting_type"],
        "notes": fields["notes"],
        "status": "confirmed" if connected else "confirmed (local record)",
        "calendar_event_id": created.get("id", "") if isinstance(created, dict) else "",
    }
    sheets_saved, sheets_note = await sheets_api.append_booking_row(booking)
    booking["sheets"] = "saved" if sheets_saved else sheets_note
    if not sheets_saved:
        booking["status"] = booking["status"] + "; sheets follow-up"
    meeting_core.append_booking_record(BASE_DIR, booking)
    booking["mail"] = "queued"
    booking["calendar"] = "saved to Google Calendar"
    asyncio.create_task(_send_meeting_confirmation_mail(booking))
    return booking


@app.post("/api/meetings/book")
async def book_meeting(request: MeetingBookingRequest) -> dict:
    """Validate, recheck the slot, create the meeting, then notify in background."""
    from app.agent import meetings as meeting_core

    try:
        fields = meeting_core.validate_booking(request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        booking = await _create_meeting_booking(fields)
    except MeetingBookingError as exc:
        if exc.code == "taken":
            raise HTTPException(status_code=409, detail="That time was just booked. Please pick another slot.") from exc
        if exc.code == "not_connected":
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if request.session_id and request.session_id.strip():
        _last_bookings[request.session_id.strip()] = booking
    speech = meeting_core.confirmation_speech(booking)
    return {"booking": booking, "message": speech + " Setting that up now: you will get confirmation once it is ready."}


@app.get("/api/meetings/bookings")
async def list_meeting_bookings() -> dict:
    """Newest-first internal booking records for the dashboard."""
    from app.agent import meetings as meeting_core

    return {"bookings": meeting_core.read_booking_records(BASE_DIR)}


@app.delete("/api/meetings/bookings/{booking_id}")
async def cancel_meeting_booking(booking_id: str) -> dict:
    """Cancel one booking: delete the Calendar event, mark the record cancelled.

    The freed slot becomes bookable again immediately. Unknown ids 404;
    already-cancelled rows return as-is.
    """
    from app.agent import meetings as meeting_core
    from app.integrations.google import calendar as calendar_api

    wanted = (booking_id or "").strip().upper()
    current: dict | None = None
    for row in meeting_core.read_booking_records(BASE_DIR, limit=1000):
        if str(row.get("booking_id", "")).upper() == wanted:
            current = row
            break
    if current is None:
        raise HTTPException(status_code=404, detail="Booking not found.")
    if str(current.get("status", "")).startswith("cancelled"):
        return {"booking": current, "calendar": "already cancelled"}
    calendar_note = "no calendar event stored"
    event_id = str(current.get("calendar_event_id") or "")
    if event_id and not _google_connected():
        calendar_note = (
            "Google is not connected, so the Calendar event could not be "
            "removed automatically. Delete it in Google Calendar, or reconnect "
            "Google and cancel again."
        )
    elif event_id and _google_connected():
        try:
            deps = _google_deps()
            async with deps.client_factory() as client:
                await calendar_api.delete_event(client, deps.ensure_token, event_id)
            calendar_note = "calendar event deleted"
        except Exception as exc:
            logger.warning("Calendar delete failed id=%s (%s).", wanted, type(exc).__name__)
            calendar_note = (
                f"Calendar delete failed ({exc}). The record is cancelled "
                "locally, but the slot stays blocked in Google Calendar until "
                "that event is removed."
            )
    updated = meeting_core.cancel_booking_record(BASE_DIR, wanted)
    if updated is None:
        raise HTTPException(status_code=404, detail="Booking not found.")
    return {"booking": updated, "calendar": calendar_note}


@app.delete("/api/meetings/bookings")
async def purge_meeting_bookings(only: str = "") -> dict:
    """Remove internal booking rows. Only supports ?only=cancelled for now."""
    from app.agent import meetings as meeting_core

    if only.strip().lower() != "cancelled":
        raise HTTPException(status_code=422, detail="Only ?only=cancelled is supported.")
    removed = meeting_core.purge_cancelled_records(BASE_DIR)
    return {"removed": removed}


@app.get("/api/meetings/sheets-link")
async def meeting_sheets_link() -> dict:
    """Google Sheet URL when Sheets is configured, else unconfigured."""
    from app.integrations.google import sheets as sheets_api

    config = sheets_api.sheets_config()
    if config is None:
        return {"configured": False, "url": None, "sheet_name": ""}
    return {
        "configured": True,
        "url": f"https://docs.google.com/spreadsheets/d/{config['spreadsheet_id']}",
        "sheet_name": config.get("sheet_name", ""),
    }


@app.get("/api/history")
async def get_conversation_history() -> dict[str, list[dict[str, str]]]:
    """Return persisted completed turns for the local conversation view."""
    return {"turns": await conversation_logs.list_turns_snapshot()}


@app.post("/api/history/turn", status_code=202)
async def log_conversation_turn(request: ConversationTurnRequest) -> dict[str, str]:
    """Enqueue one completed turn for background persistence."""
    user = request.user.strip()
    assistant = request.assistant.strip()
    if not user or not assistant:
        raise HTTPException(status_code=422, detail="A conversation turn cannot be empty.")
    result = conversation_logs.enqueue(
        request.session_id,
        request.turn_id,
        user,
        assistant,
    )
    if result == "full":
        raise HTTPException(status_code=503, detail="Conversation history is temporarily busy.")
    return {"status": result}


@app.delete("/api/history", status_code=204)
async def clear_conversation_history() -> Response:
    """Delete generated conversation logs after the browser confirms."""
    try:
        await conversation_logs.clear()
    except OSError as exc:
        logger.warning("Could not clear conversation history (%s).", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Conversation history could not be cleared.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Conversation history is unavailable.") from exc
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Google OAuth (Calendar). Tokens stay server-side in secrets/; the browser
# only sees redirects and a connected/not-connected flag.
# ---------------------------------------------------------------------------

_GOOGLE_DONE_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8" />
<title>Raala - Google connected</title></head><body style="font-family:sans-serif">
<h1>Google Calendar connected</h1>
<p>Raala can now answer Calendar questions through voice. You can close this tab.</p>
</body></html>"""


_GOOGLE_ACCESS_DENIED_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8" />
<title>Raala - Google access needed</title></head><body style="font-family:sans-serif">
<h1>Google did not authorize Raala</h1>
<p>You (or Google) declined the authorization request.</p>
<p>If you saw "Access blocked" or "access_denied", the developer must add your
Gmail address under Google Cloud Console - APIs &amp; Services - OAuth consent
screen - Test users, then you can try the login again.</p>
</body></html>"""


@app.get("/auth/google/login")
async def google_login() -> RedirectResponse:
    """Start Google OAuth login (Calendar scope, offline access)."""
    deps = _google_deps()
    if not deps.configured():
        raise HTTPException(status_code=503, detail="Google is not configured on the server.")
    from app.integrations.google import oauth

    state = oauth.create_state()
    return RedirectResponse(
        oauth.build_authorize_url(deps.client_id, deps.redirect_uri, state),
        status_code=307,
    )


@app.get("/auth/google/callback")
async def google_callback(
    code: str | None = None, state: str | None = None, error: str | None = None
):
    """Exchange the authorization code and store the refresh token."""
    from app.integrations.google import oauth, token_store

    if error:
        if error == "access_denied":
            return HTMLResponse(_GOOGLE_ACCESS_DENIED_HTML)
        raise HTTPException(status_code=400, detail=f"Google authorization failed: {error}")
    if not oauth.consume_state(state):
        raise HTTPException(
            status_code=400, detail="Invalid or expired login state. Please try again."
        )
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code.")
    deps = _google_deps()
    if not deps.configured():
        raise HTTPException(status_code=503, detail="Google is not configured on the server.")
    async with deps.client_factory() as client:
        try:
            tokens = await oauth.exchange_code(
                client, deps.client_id, deps.client_secret, deps.redirect_uri, code
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not tokens.get("refresh_token"):
        raise HTTPException(
            status_code=502,
            detail="Google did not return a refresh token. Please try again.",
        )
    token_store.save_tokens(tokens, deps.token_path)
    return HTMLResponse(_GOOGLE_DONE_HTML)


@app.get("/api/google/status")
async def google_status() -> dict:
    """Report whether a Google account is connected (no token details)."""
    from app.integrations.google import token_store

    deps = _google_deps()
    if not deps.configured():
        return {"connected": False, "configured": False}
    return {"connected": token_store.is_connected(deps.token_path), "configured": True}


@app.post("/api/google/disconnect")
async def google_disconnect() -> dict:
    """Remove the stored Google refresh token."""
    from app.integrations.google import token_store

    deps = _google_deps()
    token_store.clear_tokens(deps.token_path)
    return {"connected": False}


@app.post("/api/tts")
async def tts(request: TTSRequest) -> StreamingResponse:
    """Synthesize text and return its WAV audio."""
    from app.tts import synthesize_to_bytes

    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Text must not be empty.")

    try:
        if _tts_semaphore is None:
            wav_bytes = await asyncio.to_thread(synthesize_to_bytes, text, request.model)
        else:
            async with _tts_semaphore:
                wav_bytes = await asyncio.to_thread(synthesize_to_bytes, text, request.model)
        return StreamingResponse(
            io.BytesIO(wav_bytes),
            media_type="audio/wav",
            headers={"Content-Disposition": 'attachment; filename="voice-pilot.wav"'},
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("TTS synthesis failed.")
        raise HTTPException(status_code=500, detail="Speech synthesis failed.") from exc



@app.websocket("/api/stt")
async def stt(websocket: WebSocket) -> None:
    """Relay live 24 kHz PCM audio to Meta Muse Voice Transcribe."""
    await websocket.accept()
    api_key = os.getenv("MODEL_API_KEY", "").strip()
    if not api_key:
        await websocket.send_json(
            {"type": "error", "message": "Speech transcription is not configured on the server."}
        )
        await websocket.close(code=1011)
        return

    try:
        start_message = await asyncio.wait_for(websocket.receive_json(), timeout=10)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        await websocket.close(code=1008, reason="Send a start message to begin.")
        return
    if not isinstance(start_message, dict) or start_message.get("type") != "start":
        await websocket.send_json({"type": "error", "message": "Invalid transcription start message."})
        await websocket.close(code=1008)
        return

    session_id = uuid.uuid4().hex
    access_token = api_key if api_key.lower().startswith("bearer ") else "Bearer " + api_key
    handshake = {
        "authorization": {"accessToken": access_token},
        "audioEncoding": "PCM_24KHZ",
        "model": ASR_MODEL,
        "mode": "ENDPOINTING",
        "partialMode": "CUMULATIVE",
        "languageBias": ["English"],
        "emitAudioProgress": False,
    }

    try:
        async with websocket_connect(
            f"{ASR_URI}?sessionId={session_id}",
            open_timeout=10,
            close_timeout=5,
            max_size=ASR_MAX_FRAME_BYTES,
            ping_interval=ASR_PING_INTERVAL,
            ping_timeout=ASR_PING_TIMEOUT,
        ) as upstream:
            await upstream.send(json.dumps(handshake))
            ack = json.loads(await asyncio.wait_for(upstream.recv(), timeout=10))
            if not isinstance(ack, dict) or not ack.get("sessionId"):
                raise RuntimeError("Muse ASR handshake was rejected.")

            await websocket.send_json({"type": "ready", "sessionId": session_id})

            end_stream_sent = asyncio.Event()
            upstream_closed_unexpectedly = False

            async def end_upstream_stream() -> None:
                end_stream_sent.set()
                await upstream.send(json.dumps({"type": "endStream"}))

            async def forward_audio() -> None:
                bytes_sent = 0
                try:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            await end_upstream_stream()
                            return

                        audio = message.get("bytes")
                        if audio is not None:
                            if not audio or len(audio) > ASR_MAX_FRAME_BYTES or len(audio) % 2:
                                await websocket.send_json(
                                    {"type": "error", "message": "Invalid PCM audio frame."}
                                )
                                await end_upstream_stream()
                                return
                            bytes_sent += len(audio)
                            if bytes_sent > ASR_MAX_SESSION_BYTES:
                                await websocket.send_json(
                                    {"type": "error", "message": "Listening session reached the 10-minute limit."}
                                )
                                await end_upstream_stream()
                                return
                            await upstream.send(audio)
                            continue

                        text = message.get("text")
                        if text is not None:
                            try:
                                control = json.loads(text)
                            except json.JSONDecodeError:
                                control = {}
                            if isinstance(control, dict) and control.get("type") == "stop":
                                await end_upstream_stream()
                                return
                            await websocket.send_json(
                                {"type": "error", "message": "Unsupported transcription control message."}
                            )
                            await end_upstream_stream()
                            return
                except WebSocketDisconnect:
                    await end_upstream_stream()
                except ConnectionClosed:
                    return

            async def forward_events() -> None:
                nonlocal upstream_closed_unexpectedly
                try:
                    async for raw_event in upstream:
                        if isinstance(raw_event, bytes):
                            raw_event = raw_event.decode("utf-8")
                        try:
                            event = json.loads(raw_event)
                        except (TypeError, json.JSONDecodeError):
                            logger.warning("Muse ASR sent malformed event (session %s).", session_id)
                            continue
                        if not isinstance(event, dict):
                            continue
                        if event.get("type") == "error":
                            await websocket.send_json(
                                {
                                    "type": "error",
                                    "message": str(event.get("message", "Transcription provider error.")),
                                }
                            )
                            continue
                        await websocket.send_json(event)
                except ConnectionClosed as exc:
                    if not end_stream_sent.is_set():
                        upstream_closed_unexpectedly = True
                        logger.warning(
                            "Muse ASR closed unexpectedly (session %s, code %s, reason %s).",
                            session_id,
                            exc.code,
                            exc.reason or "none",
                        )
                        await websocket.send_json(
                            {
                                "type": "error",
                                "message": (
                                    "Speech service connection closed unexpectedly "
                                    f"(code {exc.code})."
                                ),
                            }
                        )
                    logger.debug("Muse ASR closed after endStream (session %s).", session_id)

                await websocket.send_json({"type": "sessionComplete"})
                await websocket.close(code=1011 if upstream_closed_unexpectedly else 1000)

            sender = asyncio.create_task(forward_audio())
            receiver = asyncio.create_task(forward_events())
            try:
                await asyncio.gather(sender, receiver)
            finally:
                for task in (sender, receiver):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(sender, receiver, return_exceptions=True)
    except WebSocketDisconnect:
        return
    except Exception as exc:
        logger.warning(
            "Muse ASR realtime session failed (session %s, error %s).",
            session_id,
            type(exc).__name__,
        )
        try:
            await websocket.send_json(
                {"type": "error", "message": "Could not connect to the transcription service."}
            )
            await websocket.close(code=1011)
        except WebSocketDisconnect:
            pass


@app.get("/")
def index() -> FileResponse:
    """Serve the placeholder frontend."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/dashboard")
def dashboard() -> FileResponse:
    """Serve the meeting dashboard."""
    return FileResponse(STATIC_DIR / "dashboard.html")
