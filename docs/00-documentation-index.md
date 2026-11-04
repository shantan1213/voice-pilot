# 🧭 Voice-Pilot documentation index

[![Source verified](https://img.shields.io/badge/source-verified-16A34A)](../app/main.py)
[![Architecture](https://img.shields.io/badge/architecture-FastAPI%20%2B%20WebSocket%20%2B%20SSE-0EA5E9)](01-project-overview.md)
[![Local TTS](https://img.shields.io/badge/TTS-local%20MMS-F97316)](08-text-to-speech-and-audio-playback.md)

This set documents the implementation currently in this repository. Read in numeric order for an onboarding path; the source links point to the files that were inspected.

## Documents

| File | Purpose |
|---|---|
| [01 Project overview](01-project-overview.md) | Scope, users, and repository map |
| [02 System architecture](02-system-architecture.md) | Runtime components and boundaries |
| [03 Startup and lifecycle](03-application-startup-and-lifecycle.md) | FastAPI lifespan and shutdown |
| [04 Frontend architecture](04-frontend-architecture.md) | Browser UI and state model |
| [05 Audio capture](05-audio-capture-and-processing.md) | Microphone PCM and relay |
| [06 Speech to text](06-speech-to-text.md) | Muse Voice Transcribe protocol |
| [07 LLM streaming](07-llm-and-response-streaming.md) | Muse Spark SSE handling |
| [08 TTS and playback](08-text-to-speech-and-audio-playback.md) | MMS synthesis and browser audio |
| [09 Conversation lifecycle](09-complete-conversation-lifecycle.md) | End-to-end turn sequence |
| [10 History](10-conversation-history-and-persistence.md) | Queue, files, snapshots, clearing |
| [11 Backend API](11-backend-api-reference.md) | HTTP and WebSocket reference |
| [12 Communication contracts](12-frontend-backend-communication-contracts.md) | Payloads and events |
| [13 Configuration](13-configuration-and-environment.md) | Settings and environment |
| [14 Testing](14-testing-strategy.md) | Existing tests and test seams |
| [15 Performance](15-performance-and-latency.md) | Limits and latency choices |
| [16 Security and privacy](16-security-and-privacy.md) | Secrets, data, and trust boundaries |
| [17 Errors and debugging](17-error-handling-and-debugging.md) | Failure behavior and diagnosis |
| [18 Development guide](18-development-and-extension-guide.md) | Safe extension workflow |
| [19 Glossary](19-glossary.md) | Terms used throughout |
| [20 Calendar agent tool](20-google-calendar-agent-tool.md) | Raala's Google Calendar actions, confirmation, and OAuth |
| [21 Gmail + Tasks tools](21-gmail-tasks-agent-tools.md) | Send-only Gmail and Tasks CRUD-lite, incremental auth |

## Source of truth

The implementation is in [`app/main.py`](../app/main.py), [`app/tts.py`](../app/tts.py), [`app/conversation_logs.py`](../app/conversation_logs.py), [`app/settings.py`](../app/settings.py), [`app/routers/`](../app/routers/), [`app/agent/`](../app/agent/), [`app/integrations/`](../app/integrations/), and [`static/app.js`](../static/app.js). Generated `conversation_logs/` data is runtime state, not documentation. When behavior differs from prose, verify the source and update the relevant page.
