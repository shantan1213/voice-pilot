import asyncio
import json
import os
import tempfile
import unittest
import wave
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.main as main


def decode_sse_events(body: str) -> list[dict]:
    events = []
    for frame in body.split("\n\n"):
        data = "\n".join(
            line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")
        )
        if data:
            events.append(json.loads(data))
    return events


class FakeResponse:
    def __init__(self, lines: list[str], status_code: int = 200, error_body: dict | None = None):
        self.lines = lines
        self.status_code = status_code
        self.error_body = error_body or {}
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def aread(self) -> bytes:
        return json.dumps(self.error_body).encode()

    def json(self) -> dict:
        return self.error_body

    async def aiter_lines(self):
        for line in self.lines:
            yield line


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.request_args = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.response.closed = True

    def stream(self, *args, **kwargs):
        self.request_args = (args, kwargs)
        return self.response


class ChatStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_text_and_ignores_empty_choices_and_missing_deltas(self):
        response = FakeResponse(
            [
                'data: {"choices":[]}',
                "",
                'data: {"choices":[{"delta":{}}]}',
                "",
                'data: {"choices":[{"delta":{"content":"Hello "},"finish_reason":null}]}',
                "",
                'data: {"choices":[{"delta":{"content":"there."},"finish_reason":"stop"}]}',
                "",
                "data: [DONE]",
                "",
            ]
        )
        client = FakeClient(response)

        with patch.object(main.httpx, "AsyncClient", return_value=client):
            events = [
                json.loads(event.removeprefix("data: ").strip())
                async for event in main._stream_muse_chat(
                    [{"role": "user", "content": "Say hello"}], "secret-test-key"
                )
            ]

        self.assertEqual(
            events,
            [
                {"type": "delta", "text": "Hello "},
                {"type": "delta", "text": "there."},
                {"type": "done"},
            ],
        )
        args, kwargs = client.request_args
        self.assertEqual(args, ("POST", main.CHAT_COMPLETIONS_URL))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret-test-key")
        self.assertTrue(kwargs["json"]["stream"])
        self.assertEqual(kwargs["json"]["model"], main.CHAT_MODEL)
        self.assertTrue(response.closed)

    async def test_reports_empty_model_response(self):
        response = FakeResponse(['data: {"choices":[]}', "", "data: [DONE]", ""])
        client = FakeClient(response)
        with patch.object(main.httpx, "AsyncClient", return_value=client):
            events = [
                json.loads(event.removeprefix("data: ").strip())
                async for event in main._stream_muse_chat([], "secret-test-key")
            ]
        self.assertEqual(events, [{"type": "error", "message": "Muse Spark returned an empty response."}])

    async def test_surfaces_provider_http_errors(self):
        response = FakeResponse([], status_code=401, error_body={"error": {"message": "Invalid API key"}})
        client = FakeClient(response)
        with patch.object(main.httpx, "AsyncClient", return_value=client):
            events = [
                json.loads(event.removeprefix("data: ").strip())
                async for event in main._stream_muse_chat([], "secret-test-key")
            ]
        self.assertEqual(events, [{"type": "error", "message": "Invalid API key"}])
        self.assertTrue(response.closed)

    async def test_reports_provider_timeout(self):
        class TimeoutStream:
            async def __aenter__(self):
                raise main.httpx.ReadTimeout("test timeout")

            async def __aexit__(self, *_args):
                return False

        class TimeoutClient(FakeClient):
            def stream(self, *_args, **_kwargs):
                return TimeoutStream()

        client = TimeoutClient(FakeResponse([]))
        with patch.object(main.httpx, "AsyncClient", return_value=client):
            events = [
                json.loads(event.removeprefix("data: ").strip())
                async for event in main._stream_muse_chat([], "secret-test-key")
            ]
        self.assertEqual(
            events,
            [{"type": "error", "message": "Muse Spark timed out before finishing the response."}],
        )

    async def test_cancellation_closes_provider_stream(self):
        response = FakeResponse(['data: {"choices":[{"delta":{"content":"Partial"}}]}'])
        client = FakeClient(response)
        with patch.object(main.httpx, "AsyncClient", return_value=client):
            stream = main._stream_muse_chat([], "secret-test-key")
            first = await anext(stream)
            self.assertIn('"Partial"', first)
            with self.assertRaises(asyncio.CancelledError):
                await stream.athrow(asyncio.CancelledError)
        self.assertTrue(response.closed)


class ChatEndpointTests(unittest.TestCase):
    def test_endpoint_streams_events_and_adds_server_instruction(self):
        captured_messages = None

        async def fake_stream(messages, _api_key):
            nonlocal captured_messages
            captured_messages = messages
            yield main._sse_event({"type": "delta", "text": "Hello"})
            yield main._sse_event({"type": "done"})

        with patch.dict(os.environ, {"MODEL_API_KEY": "test-key"}):
            with patch.object(main, "_stream_muse_chat", fake_stream):
                with TestClient(main.app) as client:
                    response = client.post(
                        "/api/chat/stream",
                        json={"messages": [{"role": "user", "content": "Hi"}]},
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
        self.assertEqual(
            decode_sse_events(response.text),
            [{"type": "delta", "text": "Hello"}, {"type": "done"}],
        )
        self.assertEqual(captured_messages[0]["role"], "developer")
        self.assertIn("Voice-Pilot", captured_messages[0]["content"])
        self.assertIn("Do not use Markdown", captured_messages[0]["content"])
        self.assertEqual(captured_messages[-1], {"role": "user", "content": "Hi"})

    def test_endpoint_rejects_context_over_limit(self):
        with patch.dict(os.environ, {"MODEL_API_KEY": "test-key"}):
            with TestClient(main.app) as client:
                response = client.post(
                    "/api/chat/stream",
                    json={"messages": [{"role": "user", "content": "x" * 8000}] * 4},
                )
        self.assertEqual(response.status_code, 413)


class ExistingEndpointTests(unittest.TestCase):
    def test_health_keeps_legacy_phase_and_uses_new_name(self):
        with TestClient(main.app) as client:
            response = client.get("/api/health")
        self.assertEqual(response.json(), {"status": "ok", "phase": 1, "app": "Voice-Pilot"})

    def test_tts_still_returns_a_playable_wav_response(self):
        def fake_synthesis(text, output_path, model_name="mms"):
            self.assertEqual(text, "Hello from Voice-Pilot.")
            self.assertEqual(model_name, "mms")
            with wave.open(str(output_path), "wb") as audio_file:
                audio_file.setnchannels(1)
                audio_file.setsampwidth(2)
                audio_file.setframerate(24_000)
                audio_file.writeframes(b"\x01\x00" * 256)

        with patch("app.tts.synthesize_to_wav", side_effect=fake_synthesis):
            with TestClient(main.app) as client:
                response = client.post(
                    "/api/tts",
                    json={"text": "Hello from Voice-Pilot.", "model": "mms"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "audio/wav")
        self.assertIn("voice-pilot.wav", response.headers["content-disposition"])
        self.assertEqual(response.content[:4], b"RIFF")
        self.assertEqual(response.content[8:12], b"WAVE")

    def test_stt_websocket_relays_audio_and_final_transcript(self):
        class FakeUpstream:
            def __init__(self):
                self.events = asyncio.Queue()
                self.sent = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def send(self, value):
                self.sent.append(value)
                if isinstance(value, str) and json.loads(value).get("type") == "endStream":
                    self.events.put_nowait(
                        json.dumps({"type": "speechComplete", "transcript": "Hello Voice-Pilot."})
                    )
                    self.events.put_nowait(None)

            async def recv(self):
                return json.dumps({"sessionId": "test-session"})

            async def __aiter__(self):
                while True:
                    event = await self.events.get()
                    if event is None:
                        return
                    yield event

        upstream = FakeUpstream()
        with patch.dict(os.environ, {"MODEL_API_KEY": "test-key"}):
            with patch.object(main, "websocket_connect", return_value=upstream):
                with TestClient(main.app) as client:
                    with client.websocket_connect("/api/stt") as websocket:
                        websocket.send_json({"type": "start"})
                        self.assertEqual(websocket.receive_json()["type"], "ready")
                        audio = b"\x00\x01" * 2048
                        websocket.send_bytes(audio)
                        websocket.send_json({"type": "stop"})
                        events = []
                        while True:
                            event = websocket.receive_json()
                            events.append(event)
                            if event.get("type") == "sessionComplete":
                                break

        handshake = json.loads(upstream.sent[0])
        self.assertEqual(handshake["authorization"]["accessToken"], "Bearer test-key")
        self.assertEqual(handshake["audioEncoding"], "PCM_24KHZ")
        self.assertIn(audio, upstream.sent)
        self.assertEqual(
            events,
            [
                {"type": "speechComplete", "transcript": "Hello Voice-Pilot."},
                {"type": "sessionComplete"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
