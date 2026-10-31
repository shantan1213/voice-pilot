# 🎯 Project overview

Voice-Pilot is a local-first browser voice assistant. The browser records microphone audio, the FastAPI backend relays speech recognition to Meta Muse Voice Transcribe, Muse Spark generates a response, and the backend synthesizes each response sentence with the local `facebook/mms-tts-eng` model.

## 🌐 What runs where

- **Browser:** microphone capture, WebSocket and fetch calls, sentence buffering, WAV playback, status/avatar rendering.
- **FastAPI:** serves [`static/index.html`](../static/index.html), exposes API routes, keeps provider credentials server-side, streams provider results, and queues history writes.
- **Meta services:** remote ASR over WebSocket and remote Muse Spark chat over HTTP streaming.
- **Local model:** MMS-TTS loads through Transformers and runs on CPU or CUDA.

The application is intentionally not a general-purpose account system: there is no authentication layer, database, or multi-user tenancy in the inspected source.

## 🗂️ Repository map

| Path | Responsibility |
|---|---|
| `app/main.py` | App, lifespan, canonical route implementations, provider relays |
| `app/settings.py` | `pydantic-settings` environment/.env settings |
| `app/tts.py` | MMS loading and in-memory WAV synthesis |
| `app/conversation_logs.py` | Async bounded queue and text-file persistence |
| `app/routers/*.py` | Alternate extracted router modules; see registration note |
| `static/` | HTML, CSS, and browser controller |
| `tests/` | unittest and FastAPI/TestClient coverage |
| `start.ps1` | Windows setup, checks, and startup workflow |

## 🧱 Boundaries worth knowing

The public API is local HTTP/WebSocket, but ASR and chat leave the machine. TTS does not send text to a provider. The frontend never receives `MODEL_API_KEY`; `main.py` adds authorization only when calling upstream services. See [security](16-security-and-privacy.md) and [configuration](13-configuration-and-environment.md).
