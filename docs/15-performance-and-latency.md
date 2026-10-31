# ⚡ Performance and latency

The main latency-sensitive path is listening -> final transcript -> first chat delta -> first synthesized sentence. The implementation avoids unnecessary blocking in each stage.

- A shared `httpx.AsyncClient` reuses connections; limits are 10 keep-alive and 10 total connections.
- Chat timeouts are connect 10s, read 45s, write 20s, pool 10s.
- MMS warm-up starts in the background during lifespan, so first-turn work can happen before a response; failures retry lazily.
- TTS runs in a worker thread and is limited to two concurrent syntheses by an `asyncio.Semaphore`.
- TTS returns in-memory WAV bytes rather than writing temporary files.
- History enqueue uses `put_nowait`; GET uses a disk snapshot rather than `queue.join()`.
- Chat retries one empty successful provider stream, but does not retry arbitrary text or network failures.

```mermaid
flowchart LR
  A[ASR relay] --> B[final transcript]
  B --> C[chat first delta]
  C --> D[sentence buffer]
  D --> E[TTS semaphore]
  E --> F[first WAV playback]
```

The dominant variable costs are network/provider response time and first model download/inference, especially on CPU. Keep the 1,000-character TTS limit and 24,000-character chat context limit in mind when measuring. Do not trade away cancellation, bounded queues, or feedback suppression for speculative micro-optimizations.
