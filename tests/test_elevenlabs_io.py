import asyncio
import json
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from voice.elevenlabs_io import ElevenLabsTtsError, speak_with_eleven_flash


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
    async def test_tts_no_audio_is_an_error(self):
        ws = FakeWebsocket([json.dumps({"isFinal": True})])

        async def connect(*_args, **_kwargs):
            return ws

        fake_websockets = types.SimpleNamespace(
            connect=connect,
            exceptions=types.SimpleNamespace(ConnectionClosed=FakeClosed, ConnectionClosedOK=FakeClosedOk),
        )

        with mock.patch.dict(sys.modules, {"websockets": fake_websockets}):
            with self.assertRaisesRegex(ElevenLabsTtsError, "produced no audio.*voice-123"):
                await speak_with_eleven_flash(chunks(), "key", "voice-123", asyncio.Event(), asyncio.Event())

    async def test_tts_error_message_is_an_error(self):
        ws = FakeWebsocket([json.dumps({"message": "voice not found"})])

        async def connect(*_args, **_kwargs):
            return ws

        fake_websockets = types.SimpleNamespace(
            connect=connect,
            exceptions=types.SimpleNamespace(ConnectionClosed=FakeClosed, ConnectionClosedOK=FakeClosedOk),
        )

        with mock.patch.dict(sys.modules, {"websockets": fake_websockets}):
            with self.assertRaisesRegex(ElevenLabsTtsError, "voice not found"):
                await speak_with_eleven_flash(chunks(), "key", "voice-123", asyncio.Event(), asyncio.Event())

    async def test_tts_cancel_sends_final_text_before_close(self):
        ws = BlockingWebsocket([])

        async def connect(*_args, **_kwargs):
            return ws

        fake_websockets = types.SimpleNamespace(
            connect=connect,
            exceptions=types.SimpleNamespace(ConnectionClosed=FakeClosed, ConnectionClosedOK=FakeClosedOk),
        )

        async def unfinished_chunks():
            yield "hello"
            await asyncio.Event().wait()

        with mock.patch.dict(sys.modules, {"websockets": fake_websockets}):
            task = asyncio.create_task(
                speak_with_eleven_flash(unfinished_chunks(), "key", "voice-123", asyncio.Event(), asyncio.Event())
            )
            for _ in range(10):
                if len(ws.sent) >= 2:
                    break
                await asyncio.sleep(0.01)

            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(json.loads(ws.sent[-1]), {"text": ""})
        self.assertTrue(ws.closed)


if __name__ == "__main__":
    unittest.main()
