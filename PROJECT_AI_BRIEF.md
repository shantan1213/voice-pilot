# 🤖 Voice-Pilot (Raala) — AI Handoff Brief

> **Color key:** 🎤 audio · 💬 chat · 🔊 TTS · 📝 history · ⚡ latency · 🔐 security

Copy-paste this file to any AI to discuss and plan further changes.

## 🎯 What it does

Voice-Pilot is a local-first Windows voice assistant (persona: `Raala`).
Browser mic → realtime transcription → streaming LLM reply → local TTS voice.
User taps mic, speaks, sees `agent-status` text stream, hears sentence-by-sentence
local voice. No full on-screen chat transcript by design — focused voice UI with
avatar states.

## 📌 Repo state

- Origin: https://github.com/shantan1213/voice-pilot.git
- Branch: `master` at `3053a65` (pushed to `origin/master`)
- Tests: 25 passing (`python -m unittest discover -s tests -v`)
- Run: `.\start.ps1 -SkipTests` or `.\start.bat`
- URL: `http://127.0.0.1:8002`
- Health: `GET /api/health` → `{status, phase:1, app, model_ready:bool}`

## 🧰 Stack

FastAPI + Uvicorn, `httpx.AsyncClient`, `websockets`, `python-dotenv` +
`pydantic-settings`, Torch + Transformers (`facebook/mms-tts-eng`) +
`soundfile`/`numpy`, vanilla JS + SVG/CSS, `unittest` + `TestClient`.

## 🗂️ Key files

- `app/main.py`: FastAPI app, `lifespan`, `GET /api/health`, `WS /api/stt`,
  `POST /api/chat/stream` SSE, `POST /api/tts`, `GET/POST/DELETE /api/history`
- `app/tts.py`: `synthesize_to_bytes()`, `synthesize_to_wav()`,
  `warm_mms_model()`, `is_model_ready()`, `threading.RLock`
- `app/conversation_logs.py`: `ConversationLogWorker` with `enqueue()`,
  `list_turns_snapshot()`, `list_turns()`, `clear()`
- `app/settings.py`: `BaseSettings` (`model_api_key`, `muse_chat_model`)
- `app/routers/`: `health.py`, `chat.py`, `history.py`, `tts.py`, `stt.py`
- `static/app.js`, `static/index.html`, `static/styles.css`
- `start.ps1` (hash-cached pip install), `requirements.txt`, `README.md`
- `tests/test_chat_stream.py`, `tests/test_conversation_logs.py`,
  `tests/test_backend_efficiency.py`

## 🔄 End-to-end flows

### 1. 🎤 Audio capture
`static/app.js` requests `getUserMedia` in parallel with STT handshake
(`micPromise`), `AudioContext` 24kHz mono PCM, `WS /api/stt` binary frames.
Limits: `ASR_MAX_FRAME_BYTES=64KB`, 10-min session cap.

### 2. 🗣️ STT
`app.main:stt()` accepts WS, validates `{"type":"start"}`, connects to
`wss://api.meta.ai/v1/asr/realtime?sessionId=...` with
`muse-voice-transcribe-1.0`, `PCM_24KHZ`, `ENDPOINTING`, forwards audio,
relays `speechComplete` → `sessionComplete`.

### 3. 💬 Chat
`POST /api/chat/stream` validates `MODEL_API_KEY` (env or settings),
`CHAT_MODEL=muse-spark-1.3`, `CHAT_MAX_MESSAGES=40`,
`CHAT_MAX_TOTAL_CHARACTERS=24000`.
`_stream_muse_chat()` uses shared `_chat_client_context()` with
`httpx.Limits(max_keepalive_connections=10, max_connections=10)` and
`Timeout(connect=10, read=45, write=20, pool=10)`, retries empty stream once,
emits SSE `delta/done/error`.

### 4. 🔊 TTS
Frontend splits streamed text into sentences,
`POST /api/tts {"text","model":"mms"}` →
`asyncio.to_thread(synthesize_to_bytes)` guarded by
`asyncio.Semaphore(2)` →
`StreamingResponse(io.BytesIO(wav_bytes), media_type=audio/wav)`.
Sequential playback via `AudioContext`.

### 5. 📝 History
`POST /api/history/turn {session_id,turn_id,user,assistant}` → `enqueue()`
(`queued/duplicate/full`), `GET /api/history` → `list_turns_snapshot()`
(no `queue.join`), `DELETE /api/history` clears.
Files: `conversation_logs/session-<hex>.txt` with `TURN <hex>` markers.

### 6. 🚀 Health / startup
`lifespan` starts log worker, clears logs, creates shared chat client +
semaphore, warms MMS in background.
`start.ps1` caches `requirements.txt` SHA256 in `.venv/requirements.hash`.

## 🎨 Frontend state

`#avatar[data-state=idle|listening|thinking|speaking|success|error]`
`[data-emotion=...][data-face=desi|robo]`, 13 emotions, `iris-group` gaze
(idle off-center, speaking centered), cowboy hat, mustache/beard variants,
ear wiggle on listening, warm theme, Meta infinity loops + sparks background,
`localStorage voicePilotFace`.

## ⚡ Constraints for planning

- Keep latency wins: client reuse, parallel mic, early first clause, in-memory
  TTS, snapshot history.
- Keep `model_ready` health shape.
- Keep tests green (auth headers are real `Bearer <key>`, not redacted).
- `start.bat` is untracked.
- Discuss next: full `APIRouter` mount in `main.py`, frontend/backend contract
  changes, new emotions/faces, persistence format.
