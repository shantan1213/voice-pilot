# 🤝 Frontend/backend communication contracts

These are the contracts enforced by the current validators and relay code.

## 💬 Chat SSE

Request content type is JSON. `messages` has 1-40 entries; each role is `user` or `assistant`, and content is 1-8,000 characters. An optional `session_id` string scopes write confirmations (event, email, task) to the browser tab. The stream uses blank-line-delimited SSE frames. The twelve agent actions and their argument shapes are enumerated in [21 Gmail + Tasks tools](21-gmail-tasks-agent-tools.md) and [20 Calendar agent tool](20-google-calendar-agent-tool.md).

```text
data: {"type":"delta","text":"Hello "}

data: {"type":"done"}
```

Errors use `{"type":"error","message":"..."}`. The frontend must tolerate deltas arriving in multiple frames and stop consuming after done/error.

## 🔊 TTS

```json
{"text":"A complete sentence.","model":"mms"}
```

Success is raw `audio/wav`, not JSON. The browser reads the response as an `ArrayBuffer`, decodes it through `AudioContext.decodeAudioData()`, and requests sentences sequentially.

## 🎤 STT WebSocket

Client sends `start`, binary frames, and `stop`. Server sends `ready` only after upstream acknowledgement. Provider-shaped JSON is forwarded, including `speechComplete` with `transcript`; server-generated `error` and terminal `sessionComplete` are stable lifecycle events. Binary frames must be nonempty, even-length, and at most 65,536 bytes.

## 📝 History

```json
{"session_id":"UUID","turn_id":"UUID","user":"...","assistant":"..."}
```

`202` means accepted by the in-process queue, not necessarily flushed to disk. A repeated pair returns `{"status":"duplicate"}`. History records returned by GET include `session_id`, `turn_id`, `timestamp`, `user`, and `assistant`.

Source: Pydantic models and route implementations in [`app/main.py`](../app/main.py), plus browser handling in [`static/app.js`](../static/app.js).
