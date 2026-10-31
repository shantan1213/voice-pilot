# 🚀 Application startup and lifecycle

FastAPI uses the `lifespan` context manager in [`app/main.py`](../app/main.py). Uvicorn enters it before serving requests and exits it during shutdown.

```mermaid
sequenceDiagram
  participant U as Uvicorn
  participant L as lifespan
  participant H as history worker
  participant C as httpx client
  participant T as MMS warmup
  U->>L: enter
  L->>H: start(), then clear files
  L->>C: create shared AsyncClient
  L->>L: create TTS semaphore (2)
  L->>T: background warm_mms_model()
  L-->>U: yield; serve requests
  U->>L: exit
  L->>T: cancel and await
  L->>C: close
  L->>H: stop and drain
```

Startup starts the history worker, clears existing `session-*.txt` records, creates the shared chat client with configured timeout/connection limits, sets `_tts_semaphore` to two concurrent syntheses, and schedules MMS loading in a background task. Warm-up failure is logged and the first TTS request retries model loading.

Shutdown cancels warm-up, closes the client, and stops the writer. Tests that use `TestClient(app)` enter this lifecycle automatically. Lifespan state is process-local; a second worker process would have its own queue and model.

See the exact implementation in [`lifespan`](../app/main.py) and the worker in [`ConversationLogWorker`](../app/conversation_logs.py).
