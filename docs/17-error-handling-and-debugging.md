# 🩺 Error handling and debugging

Errors are translated at boundaries while preserving useful diagnostics server-side.

| Area | User-visible behavior | Where to inspect |
|---|---|---|
| Missing key | 503 chat or STT error/close | `.env`, startup environment |
| ASR bad frame/start | WebSocket error and end/close | browser console, Uvicorn log |
| ASR upstream failure | error event, terminal session | Uvicorn warning with exception type |
| Chat HTTP/provider failure | SSE error event | provider status/message and server log |
| Chat timeout/request error | SSE timeout/reachability message | network and Uvicorn output |
| TTS validation | 422 JSON detail | request text/model |
| TTS/model failure | 500 generic detail; exception logged | model cache, CUDA, server log |
| History queue full | 503 | queue pressure and filesystem |

Start with health:

```powershell
Invoke-RestMethod http://127.0.0.1:8002/api/health
```

Check that `model_ready` becomes true after warm-up. Test chat independently with a small SSE request and TTS with a short sentence; then test microphone permissions in Chrome/Edge. Never diagnose by printing credentials. The canonical route is `app.main:app`; remember that `app/routers` is not included by current composition.

For lifecycle bugs, inspect cancellation and the `ready`/`sessionComplete` order. For missing history, remember that POST is asynchronous and startup deliberately clears files. Existing focused tests provide reproducible seams; see [testing](14-testing-strategy.md).
