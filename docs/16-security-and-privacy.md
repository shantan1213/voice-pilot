# 🔐 Security and privacy

The app is designed for local development, not as a hardened public service. By default the documented command binds to loopback. Do not expose it directly to an untrusted network without adding authentication, origin policy, rate limiting, and deployment controls.

## 🛡️ Secrets and data

`MODEL_API_KEY` is read server-side and sent as a bearer credential to Meta. It is not embedded in `static/app.js`, returned in API responses, or written to conversation logs. Error logging records exception types and bounded provider messages; inspect logs before sharing them because provider messages may contain account details.

Microphone audio is relayed to Meta ASR. User/assistant text is sent to Muse Spark. Completed text is written locally under `conversation_logs/`; startup clears prior session files. Raw audio, interim transcripts, tokens, and API keys are not persisted by the application. Provider retention and account terms still apply.

Input controls include Pydantic lengths, UUID parsing, accepted roles/models, PCM frame/session limits, and bounded queue capacity. TTS uses in-memory bytes. Log readers skip symlinks and malformed records.

## 🧭 Operational guidance

Keep `.env` private, use least-privilege provider credentials, avoid logging request headers, and verify the host/port before opening the UI. Review the MMS model card/license and Meta terms before redistribution. Source: [`main.py`](../app/main.py), [`settings.py`](../app/settings.py), and [`conversation_logs.py`](../app/conversation_logs.py).
