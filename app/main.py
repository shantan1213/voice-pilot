"""Voice-Pilot backend for streaming speech, chat, and local TTS."""

import asyncio
import json
import logging
import os
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
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed

from app.conversation_logs import ConversationLogWorker

load_dotenv()

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
CHAT_MODEL = os.getenv("MUSE_CHAT_MODEL", "muse-spark-1.3").strip()
CHAT_MAX_MESSAGES = 40
CHAT_MAX_TOTAL_CHARACTERS = 24_000
CHAT_TIMEOUT = httpx.Timeout(connect=10, read=90, write=20, pool=10)
CONVERSATION_LOG_DIR = BASE_DIR / "conversation_logs"
conversation_logs = ConversationLogWorker(CONVERSATION_LOG_DIR)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await conversation_logs.start()
    await conversation_logs.clear()
    try:
        yield
    finally:
        await conversation_logs.stop()


app = FastAPI(title="Voice-Pilot", version="1.0.0", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class TTSRequest(BaseModel):
    """Request body for local text-to-speech."""

    text: str = Field(min_length=1, max_length=1000)
    model: Literal["mms", "pocket-indian"] = "mms"


class ChatMessage(BaseModel):
    """A complete turn sent as context to Muse Spark."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8_000)


class ChatRequest(BaseModel):
    """Conversation context for one streamed Muse Spark response."""

    messages: list[ChatMessage] = Field(min_length=1, max_length=CHAT_MAX_MESSAGES)


class ConversationTurnRequest(BaseModel):
    """A completed, uniquely identified conversation turn to log."""

    session_id: UUID
    turn_id: UUID
    user: str = Field(min_length=1, max_length=8_000)
    assistant: str = Field(min_length=1, max_length=8_000)


@app.get("/api/health")
def health() -> dict:
    """Liveness check for the backend."""
    return {"status": "ok", "phase": 1, "app": "Voice-Pilot"}


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
    headers = {
        "Authorization": api_key if api_key.lower().startswith("bearer ") else f"Bearer {api_key}",
        "Accept": "text/event-stream",
    }
    payload = {
        "model": CHAT_MODEL,
        "messages": messages,
        "stream": True,
        "max_completion_tokens": 1024,
    }
    has_text = False
    stream_finished = False

    try:
        async with httpx.AsyncClient(timeout=CHAT_TIMEOUT) as client:
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
                    if raw_data == "[DONE]":
                        stream_finished = True
                        break
                    try:
                        chunk = json.loads(raw_data)
                    except json.JSONDecodeError:
                        logger.warning("Muse Spark sent a malformed streaming event.")
                        continue
                    if not isinstance(chunk, dict):
                        continue
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
                        continue
                    choice = choices[0]
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta")
                    content = delta.get("content") if isinstance(delta, dict) else None
                    if isinstance(content, str) and content:
                        has_text = True
                        yield _sse_event({"type": "delta", "text": content})
                    if choice.get("finish_reason") is not None:
                        stream_finished = True

                if data_lines and not stream_finished:
                    raw_data = "\n".join(data_lines)
                    if raw_data == "[DONE]":
                        stream_finished = True
                    else:
                        try:
                            chunk = json.loads(raw_data)
                        except json.JSONDecodeError:
                            chunk = {}
                        choices = chunk.get("choices") if isinstance(chunk, dict) else None
                        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                            choice = choices[0]
                            delta = choice.get("delta")
                            content = delta.get("content") if isinstance(delta, dict) else None
                            if isinstance(content, str) and content:
                                has_text = True
                                yield _sse_event({"type": "delta", "text": content})
                            if choice.get("finish_reason") is not None:
                                stream_finished = True

        if not has_text:
            yield _sse_event({"type": "error", "message": "Muse Spark returned an empty response."})
        elif not stream_finished:
            yield _sse_event(
                {"type": "error", "message": "The response stream ended before Muse Spark finished."}
            )
        else:
            yield _sse_event({"type": "done"})
    except asyncio.CancelledError:
        raise
    except httpx.TimeoutException:
        yield _sse_event({"type": "error", "message": "Muse Spark timed out before finishing the response."})
    except httpx.RequestError as exc:
        logger.warning("Muse Spark request failed (%s).", type(exc).__name__)
        yield _sse_event({"type": "error", "message": "Could not reach Muse Spark."})
    except Exception as exc:
        logger.exception("Muse Spark stream failed (%s).", type(exc).__name__)
        yield _sse_event({"type": "error", "message": "Muse Spark could not complete the response."})


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
                "You are Voice-Pilot, a friendly voice assistant. Answer in clear, "
                "natural spoken language using short conversational paragraphs. "
                "Do not use Markdown, bullet points, numbered lists, or decorative "
                "symbols; say list items as connected sentences. Keep the answer "
                "concise unless the user asks for detail."
            ),
        },
        *[message.model_dump() for message in request.messages],
    ]
    return StreamingResponse(
        _stream_muse_chat(messages, api_key),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/history")
async def get_conversation_history() -> dict[str, list[dict[str, str]]]:
    """Return persisted completed turns for the local conversation view."""
    return {"turns": await conversation_logs.list_turns()}


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


@app.post("/api/tts", response_class=FileResponse)
def tts(request: TTSRequest) -> FileResponse:
    """Synthesize text and return its WAV audio."""
    from app.tts import synthesize_to_wav

    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Text must not be empty.")

    temp_dir: Path | None = None
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="voice-pilot-tts-"))
        audio_path = temp_dir / "speech.wav"
        synthesize_to_wav(text, audio_path, model_name=request.model)
        return FileResponse(
            path=audio_path,
            media_type="audio/wav",
            filename="voice-pilot.wav",
            background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
        )
    except Exception as exc:
        logger.exception("TTS synthesis failed.")
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500, detail="Speech synthesis failed."
        ) from exc


@app.post("/api/tts/stream")
def tts_stream(request: TTSRequest) -> StreamingResponse:
    """Stream Pocket TTS as raw 24 kHz mono PCM16 audio."""
    if request.model != "pocket-indian":
        raise HTTPException(
            status_code=400,
            detail="Streaming speech is only available for the Pocket Indian-English voice.",
        )

    from app.tts import stream_pocket_pcm16

    try:
        audio_stream = stream_pocket_pcm16(request.text)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return StreamingResponse(
        audio_stream,
        media_type="audio/pcm;rate=24000;channels=1",
        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
    )


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
        ) as upstream:
            await upstream.send(json.dumps(handshake))
            ack = json.loads(await asyncio.wait_for(upstream.recv(), timeout=10))
            if not isinstance(ack, dict) or not ack.get("sessionId"):
                raise RuntimeError("Muse ASR handshake was rejected.")

            await websocket.send_json({"type": "ready", "sessionId": session_id})

            end_stream_sent = asyncio.Event()

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
                await websocket.close(code=1000)

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
