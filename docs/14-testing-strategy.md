# 🧪 Testing strategy

The repository uses the standard `unittest` runner and FastAPI `TestClient`; no browser automation suite is present. Run:

```powershell
python -m unittest discover -s tests -v
```

## ✅ Existing coverage

- [`test_chat_stream.py`](../tests/test_chat_stream.py) exercises SSE parsing, empty-choice retry, legacy text shape, truncation, provider errors/timeouts, cancellation, shared client reuse, validation, and endpoint developer instructions.
- [`test_backend_efficiency.py`](../tests/test_backend_efficiency.py) checks health readiness, timeout/connection limits, snapshot history, in-memory TTS, settings, and semaphore-related implementation seams.
- [`test_conversation_logs.py`](../tests/test_conversation_logs.py) checks ordered writes, deduplication after restart, clear ordering, and worker recovery after write errors.

Tests patch provider clients and TTS rather than making network/model calls. `TestClient` enters lifespan, which means tests can observe startup warm-up scheduling without downloading a model when the relevant function is patched. New route tests should use explicit environment patches and restore globals.

When adding behavior, test both the success event sequence and bounded/failed input. For streaming code, verify cancellation closes upstream resources. For history, distinguish snapshot (nonblocking) from drain (ordering guarantee).
