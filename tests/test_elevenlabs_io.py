import asyncio
import json
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from voice.assistant import VoiceSwitch
from voice.elevenlabs_io import speak_with_eleven_flash, stream_audio_to_scribe


class FakeClosed(Exception):
    code = 1006
    reason = "closed"


class FakeClosedOk(FakeClosed):
    pass


class FakeWebsocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = False

    async def send(self, message):
        self.sent.append(message)

    async def recv(self):
        return self.messages.pop(0)

    async def close(self):
        self.closed = True


class FailingTextSendWebsocket(FakeWebsocket):
    async def send(self, message):
        self.sent.append(message)
        if json.loads(message).get("text") != " ":
            raise FakeClosed()

    async def recv(self):
        await asyncio.Event().wait()


class FakeScribeWebsocket:
    def __init__(self, messages, fail_after_messages=False):
        self.messages = list(messages)
        self.fail_after_messages = fail_after_messages
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def recv(self):
        return json.dumps({"message_type": "session_started"})

    async def send(self, message):
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.messages:
            return self.messages.pop(0)
        if self.fail_after_messages:
            raise FakeClosed()
        await asyncio.Event().wait()


class BlockingWebsocket(FakeWebsocket):
    async def recv(self):
        await asyncio.Event().wait()


async def chunks():
    yield "hello"


class ElevenLabsIoTest(unittest.IsolatedAsyncioTestCase):
    async def test_tts_sends_api_key_in_initial_message(self):
        ws = FakeWebsocket([json.dumps({"isFinal": True})])

        async def connect(*_args, **_kwargs):
            return ws

        fake_websockets = types.SimpleNamespace(
            connect=connect,
            exceptions=types.SimpleNamespace(ConnectionClosed=FakeClosed, ConnectionClosedOK=FakeClosedOk),
        )

        with mock.patch.dict(sys.modules, {"websockets": fake_websockets}):
            await speak_with_eleven_flash(chunks(), "key", "voice-123", asyncio.Event(), asyncio.Event())

        self.assertEqual(json.loads(ws.sent[0])["xi_api_key"], "key")

    async def test_tts_prewarms_socket_before_first_text_chunk(self):
        ws = FakeWebsocket([json.dumps({"isFinal": True})])
        connected = asyncio.Event()

        async def connect(*_args, **_kwargs):
            connected.set()
            return ws

        fake_websockets = types.SimpleNamespace(
            connect=connect,
            exceptions=types.SimpleNamespace(ConnectionClosed=FakeClosed, ConnectionClosedOK=FakeClosedOk),
        )

        async def delayed_chunks():
            await connected.wait()
            yield "hello"

        with mock.patch.dict(sys.modules, {"websockets": fake_websockets}):
            await speak_with_eleven_flash(delayed_chunks(), "key", "voice-123", asyncio.Event(), asyncio.Event())

        self.assertEqual(json.loads(ws.sent[1]), {"text": "hello", "try_trigger_generation": True})

    async def test_tts_cancel_closes_prewarmed_socket_before_text(self):
        ws = FakeWebsocket([])
        connected = asyncio.Event()

        async def connect(*_args, **_kwargs):
            connected.set()
            return ws

        fake_websockets = types.SimpleNamespace(
            connect=connect,
            exceptions=types.SimpleNamespace(ConnectionClosed=FakeClosed, ConnectionClosedOK=FakeClosedOk),
        )

        async def waiting_chunks():
            await asyncio.Event().wait()
            yield "never"

        with mock.patch.dict(sys.modules, {"websockets": fake_websockets}):
            task = asyncio.create_task(
                speak_with_eleven_flash(waiting_chunks(), "key", "voice-123", asyncio.Event(), asyncio.Event())
            )
            await connected.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(ws.closed)

    async def test_tts_prewarm_failure_is_ignored_when_no_text_arrives(self):
        connected = asyncio.Event()

        async def connect(*_args, **_kwargs):
            connected.set()
            raise RuntimeError("connect failed")

        fake_websockets = types.SimpleNamespace(
            connect=connect,
            exceptions=types.SimpleNamespace(ConnectionClosed=FakeClosed, ConnectionClosedOK=FakeClosedOk),
        )

        async def no_chunks():
            await connected.wait()
            if False:
                yield "never"

        with mock.patch.dict(sys.modules, {"websockets": fake_websockets}):
            await speak_with_eleven_flash(no_chunks(), "key", "voice-123", asyncio.Event(), asyncio.Event())

    async def test_tts_retries_failed_prewarm_when_text_arrives(self):
        ws = FakeWebsocket([json.dumps({"isFinal": True})])
        calls = 0

        async def connect(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("connect failed")
            return ws

        fake_websockets = types.SimpleNamespace(
            connect=connect,
            exceptions=types.SimpleNamespace(ConnectionClosed=FakeClosed, ConnectionClosedOK=FakeClosedOk),
        )

        with mock.patch.dict(sys.modules, {"websockets": fake_websockets}):
            await speak_with_eleven_flash(chunks(), "key", "voice-123", asyncio.Event(), asyncio.Event())

        self.assertEqual(calls, 2)
        self.assertEqual(json.loads(ws.sent[1]), {"text": "hello", "try_trigger_generation": True})

    async def test_tts_retries_failed_text_send(self):
        sockets = [
            FailingTextSendWebsocket([]),
            FakeWebsocket([json.dumps({"isFinal": True})]),
        ]
        calls = 0

        async def connect(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return sockets[calls - 1]

        fake_websockets = types.SimpleNamespace(
            connect=connect,
            exceptions=types.SimpleNamespace(ConnectionClosed=FakeClosed, ConnectionClosedOK=FakeClosedOk),
        )

        with mock.patch.dict(sys.modules, {"websockets": fake_websockets}):
            await speak_with_eleven_flash(chunks(), "key", "voice-123", asyncio.Event(), asyncio.Event())

        self.assertEqual(calls, 2)
        self.assertTrue(sockets[0].closed)
        self.assertEqual(json.loads(sockets[1].sent[1]), {"text": "hello", "try_trigger_generation": True})

    async def test_tts_switch_voice_reopens_socket(self):
        sockets = [
            FakeWebsocket([json.dumps({"isFinal": True})]),
            FakeWebsocket([json.dumps({"isFinal": True})]),
        ]
        connected_urls = []

        async def connect(url, **_kwargs):
            connected_urls.append(url)
            return sockets[len(connected_urls) - 1]

        fake_websockets = types.SimpleNamespace(
            connect=connect,
            exceptions=types.SimpleNamespace(ConnectionClosed=FakeClosed, ConnectionClosedOK=FakeClosedOk),
        )

        async def switching_chunks():
            yield "hello"
            yield VoiceSwitch("voice-456", "alternate")
            yield "there"

        with mock.patch.dict(sys.modules, {"websockets": fake_websockets}):
            await speak_with_eleven_flash(switching_chunks(), "key", "voice-123", asyncio.Event(), asyncio.Event())

        self.assertIn("/voice-123/", connected_urls[0])
        self.assertIn("/voice-456/", connected_urls[1])
        self.assertEqual(json.loads(sockets[0].sent[-1]), {"text": ""})
        self.assertTrue(sockets[0].closed)

    async def test_scribe_reconnects_after_websocket_failure(self):
        committed = json.dumps({"message_type": "committed_transcript", "text": "hello"})
        sockets = [
            FakeScribeWebsocket([], fail_after_messages=True),
            FakeScribeWebsocket([committed]),
        ]

        def connect(*_args, **_kwargs):
            return sockets.pop(0)

        fake_websockets = types.SimpleNamespace(connect=connect)
        scribe_events = asyncio.Queue()

        async def audio_chunks():
            while True:
                await asyncio.sleep(0)
                yield b"\x01\x00" * 160

        with (
            mock.patch.dict(sys.modules, {"websockets": fake_websockets}),
            mock.patch("voice.elevenlabs_io.SCRIBE_RECONNECT_BASE_SECS", 0),
        ):
            task = asyncio.create_task(stream_audio_to_scribe(audio_chunks(), scribe_events, "key"))
            try:
                event = await asyncio.wait_for(scribe_events.get(), timeout=1.0)
                while event["type"] != "commit":
                    event = await asyncio.wait_for(scribe_events.get(), timeout=1.0)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertEqual(event, {"type": "commit", "text": "hello"})


if __name__ == "__main__":
    unittest.main()
