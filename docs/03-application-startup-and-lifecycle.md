# 🚀 Application startup and lifecycle

FastAPI uses the `lifespan` context manager in [`app/main.py`](../app/main.py). Uvicorn enters it before serving requests and exits it during shutdown.

```mermaid
sequenceDiagram
  participant UV as Uvicorn
  participant LIFE as lifespan
  participant HIST as history worker
  participant CLIENT as httpx client
  participant WARM as MMS warmup
  UV->>LIFE: enter lifespan
  LIFE->>HIST: start and clear files
  LIFE->>CLIENT: create shared AsyncClient
  LIFE->>LIFE: create TTS limit
  LIFE->>WARM: start background warm_mms_model
  LIFE-->>UV: yield and serve requests
  UV->>LIFE: exit lifespan
  LIFE->>WARM: cancel and await
  LIFE->>CLIENT: close client
  LIFE->>HIST: stop and drain
```

Startup starts the history worker, clears existing `session-*.txt` records, creates the shared chat client with configured timeout/connection limits, sets `_tts_semaphore` to two concurrent syntheses, and schedules MMS loading in a background task. Warm-up failure is logged and the first TTS request retries model loading.

Shutdown cancels warm-up, closes the client, and stops the writer. Tests that use `TestClient(app)` enter this lifecycle automatically. Lifespan state is process-local; a second worker process would have its own queue and model.

See the exact implementation in [`lifespan`](../app/main.py) and the worker in [`ConversationLogWorker`](../app/conversation_logs.py).
