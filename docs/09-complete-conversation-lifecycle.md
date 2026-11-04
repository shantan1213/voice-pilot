# 🔄 Complete conversation lifecycle

A turn crosses four streaming boundaries: browser capture, remote ASR, remote chat, and local TTS.

```mermaid
sequenceDiagram
  participant U as User
  participant B as Browser
  participant F as FastAPI
  participant ASR as Muse ASR
  participant SPARK as Muse Spark
  participant MMS as MMS
  participant HIST as history worker
  U->>B: speak
  B->>F: start and PCM
  F->>ASR: handshake and PCM
  ASR-->>B: interim transcript
  U->>B: stop speaking
  ASR-->>B: speechComplete transcript
  B->>F: chat context
  F->>SPARK: streamed completion
  SPARK-->>B: delta SSE events
  B->>F: sentence request
  F->>MMS: local synthesis
  MMS-->>B: WAV
  B->>B: sequential playback
  B->>F: completed turn
  F->>HIST: enqueue and return 202
```

The frontend retains up to `MAX_CONTEXT_TURNS` (currently 3) completed turns for context, while the server enforces the 40-message and 24,000-character request limits. A generated turn is persisted only after assistant text is complete. Capture and the STT socket are stopped when speech is finalized, before the assistant starts thinking, and Stop response aborts active work so a new turn does not overlap old playback. Listening must be started explicitly for the next turn.

Startup clears previous log files, so history is per server run. `/api/history` is a disk snapshot and may briefly lag a just-enqueued turn; `POST /api/history/turn` is intentionally asynchronous. Agent turns (Calendar, Gmail, Tasks) add a gate between chat context and the streamed completion: selection, validated registry execution (or a confirmation proposal for writes), then a verbalized answer. A confirmed write executes only after the exact "confirm" reply and only against Google's live response.
