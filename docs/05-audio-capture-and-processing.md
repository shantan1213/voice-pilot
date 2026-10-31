# 🎤 Audio capture and processing

The browser supplies microphone audio to the `/api/stt` WebSocket as binary PCM frames. The backend handshake tells Meta that the encoding is `PCM_24KHZ`, so frames are expected to be mono 24 kHz signed PCM (two bytes per sample). The server validates each received binary frame before forwarding it.

## 📡 Limits and relay

`ASR_MAX_FRAME_BYTES` is 64 KiB. Empty frames, odd byte lengths, oversized frames, and a session over `ASR_MAX_SESSION_BYTES` (24,000 samples/second * 2 bytes * 600 seconds) cause an error and upstream `endStream`. A text control message is accepted only when its JSON type is `stop`; other controls are rejected.

```mermaid
flowchart TD
  G[getUserMedia] --> P[Audio processing in app.js]
  P -->|binary PCM frame| W[/api/stt WebSocket]
  W --> V{frame valid?}
  V -- no --> E[error event and endStream]
  V -- yes --> R[forward to Meta ASR]
  R --> X[transcript events]
```

Capture should be stopped or muted while TTS plays. This is a browser feedback concern, not a server-side audio filter. The backend does not persist raw audio or interim text. Source: [`stt()`](../app/main.py), [`static/app.js`](../static/app.js), and constants near the top of `main.py`.
