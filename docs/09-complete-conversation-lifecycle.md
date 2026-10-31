# 🔄 Complete conversation lifecycle

A turn crosses four streaming boundaries: browser capture, remote ASR, remote chat, and local TTS.

```mermaid
sequenceDiagram
  participant U as User
  participant B as Browser
  participant F as FastAPI
  participant A as Muse ASR
  participant L as Muse Spark
  participant T as MMS
  participant H as history worker
  U->>B: speak
  B->>F: start + PCM
  F->>A: handshake + PCM
  A-->>B: interim transcript
  U->>B: stop speaking
  A-->>B: speechComplete transcript
  B->>F: chat context
  F->>L: streamed completion
  L-->>B: delta SSE events
  B->>F: one sentence at a time
  F->>T: local synthesis
  T-->>B: WAV
  B->>B: sequential playback
  B->>F: completed turn
  F->>H: enqueue; return 202
```

The frontend retains up to `MAX_CONTEXT_TURNS` (currently 3) completed turns for context, while the server enforces the 40-message and 24,000-character request limits. A generated turn is persisted only after assistant text is complete. Capture is stopped while the assistant speaks, and Stop response aborts active work so a new turn does not overlap old playback.

Startup clears previous log files, so history is per server run. `/api/history` is a disk snapshot and may briefly lag a just-enqueued turn; `POST /api/history/turn` is intentionally asynchronous.
