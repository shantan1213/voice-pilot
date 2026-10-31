# 🔌 Backend API reference

Routes below are implemented directly in [`app/main.py`](../app/main.py). Extracted `app/routers` modules are not included with `app.include_router`, so they are not the active registration path.

## 🌐 HTTP

| Method/path | Request | Response |
|---|---|---|
| `GET /` | none | `static/index.html` |
| `GET /api/health` | none | `200` JSON: `status`, `phase`, `app`, `model_ready` |
| `POST /api/chat/stream` | `{messages:[{role:"user"|"assistant",content:string}]}` | `text/event-stream`; `delta`, `done`, or `error` events |
| `POST /api/tts` | `{text:string,model:"mms"}` | `audio/wav`; validation errors 422, synthesis errors 500 |
| `GET /api/history` | none | `{turns:[...]}` snapshot |
| `POST /api/history/turn` | UUID `session_id`, UUID `turn_id`, nonempty user/assistant | `202 {status}`; 503 if queue full |
| `DELETE /api/history` | none | `204`; 500/503 on clear failure |

Chat returns 503 without an API key, 413 over total context, and validates message counts/content through Pydantic. TTS strips text and limits it to 1,000 characters.

## 🔗 WebSocket

`/api/stt` starts with JSON `{"type":"start"}`, then accepts binary PCM or JSON `{"type":"stop"}`. It sends `ready`, provider transcript events, `error`, and `sessionComplete`. Missing configuration closes with code 1011; invalid start/control uses 1008.

Static assets are mounted at `/static`. For exact field constraints and event examples, see [communication contracts](12-frontend-backend-communication-contracts.md).
