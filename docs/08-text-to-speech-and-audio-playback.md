# 🔊 Text-to-speech and audio playback

The browser sends each completed sentence to `POST /api/tts`. The request permits `model: "mms"` and text from 1 through 1,000 characters. The endpoint calls [`synthesize_to_bytes`](../app/tts.py) in `asyncio.to_thread`, protected by the lifespan semaphore (two concurrent syntheses), and returns `audio/wav` with an attachment filename.

MMS model ID is `facebook/mms-tts-eng`. It is loaded once, on startup warm-up or first request, onto CUDA when available and otherwise CPU. Synthesis tokenizes each sentence, runs inference mode, inserts 0.15 seconds of silence between sentence waveforms, and writes PCM-16 WAV into `BytesIO`; it does not create a temporary audio file. The model uses configured speaking/noise parameters in `tts.py`.

```mermaid
sequenceDiagram
  participant B as Browser sentence queue
  participant API as TTS endpoint
  participant LIMIT as TTS limit
  participant MMS as MMS voice model
  B->>API: JSON sentence
  API->>LIMIT: acquire capacity
  LIMIT->>MMS: synthesize in worker thread
  MMS-->>API: WAV bytes
  API-->>B: WAV audio
  B->>B: decodeAudioData and BufferSource playback
  B->>API: next sentence request
```

The client serializes playback so sentences retain order and can be interrupted. A first-use model download/load can dominate latency; `/api/health` exposes `model_ready` for warm-up visibility. See [performance](15-performance-and-latency.md).
