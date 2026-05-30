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
from voice.elevenlabs_io import speak_with_eleven_flash


class FakeClosed(Exception):
    pass


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


if __name__ == "__main__":
    unittest.main()
