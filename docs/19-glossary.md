# 📚 Glossary

- **ASR:** Automatic speech recognition; here Meta Muse Voice Transcribe.
- **Cumulative partial:** An interim transcript mode where successive events represent the accumulated utterance.
- **Delta:** A chunk of generated assistant text in a streaming response.
- **ENDPOINTING:** ASR mode used by the upstream handshake to detect utterance boundaries.
- **FastAPI lifespan:** Startup/shutdown async context surrounding request service.
- **MMS-TTS:** Meta Multilingual Speech text-to-speech checkpoint used locally as `facebook/mms-tts-eng`.
- **PCM:** Pulse-code modulation; the binary audio format relayed to ASR, configured as 24 kHz.
- **SSE:** Server-Sent Events, the `text/event-stream` format used for chat output.
- **Snapshot history:** A nonblocking read of current files that does not wait for queued writes.
- **Speech complete:** Provider event carrying the finalized transcript.
- **TTS:** Text-to-speech; conversion of assistant sentence text into WAV audio.
- **Turn ID:** UUID identifying one completed user/assistant exchange for deduplication.
- **Warm-up:** Background MMS model loading during application startup.
- **WebSocket relay:** The backend forwards browser frames/events between the client and Meta ASR without persisting audio.

For implementation details, use the cross-links in the [documentation index](00-documentation-index.md).
