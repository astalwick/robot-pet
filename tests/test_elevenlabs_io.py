import asyncio
import base64
import json
import os
import sys
import types
import unittest
from contextlib import suppress
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from voice.assistant import VoiceSwitch
from voice.elevenlabs_io import speak_with_eleven_flash, stream_audio_to_scribe
from voice.usage import UsageTotals


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
        self.assertEqual(
            json.loads(ws.sent[0])["generation_config"]["chunk_length_schedule"],
            [50, 120, 250, 290],
        )

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

        self.assertEqual(json.loads(ws.sent[1]), {"text": "hello"})

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
        self.assertEqual(json.loads(ws.sent[1]), {"text": "hello"})

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
        self.assertEqual(json.loads(sockets[1].sent[1]), {"text": "hello"})

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


# --- Speech-triggered Scribe streaming ---------------------------------------

SAMPLES = 320
LOUD = (20480).to_bytes(2, "little") * SAMPLES  # rms 20480, well above the 100 gate
SOFT = (50).to_bytes(2, "little") * SAMPLES  # rms 50, below the gate but non-silent
QUIET = b"\x00\x00" * SAMPLES  # rms 0
CHUNK_SECS = SAMPLES / 16000
WAKE = (300).to_bytes(2, "little") * SAMPLES  # distinct payload standing in for buffered wake audio

STOP = object()
FAIL = object()


def committed_message(text):
    return json.dumps({"message_type": "committed_transcript", "text": text})


def decode_payload(message):
    return base64.b64decode(json.loads(message)["audio_base_64"])


def is_silent(message):
    return set(decode_payload(message)) <= {0}


class FakeScribe:
    """A Scribe websocket the test drives directly: deliver transcripts, fail, or
    close on demand, and inspect what was uploaded."""

    def __init__(self):
        self.inbox = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def recv(self):
        return json.dumps({"message_type": "session_started"})

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.inbox.get()
        if message is FAIL:
            raise FakeClosed()
        if message is STOP:
            raise StopAsyncIteration
        return message

    def deliver(self, message):
        self.inbox.put_nowait(message)

    def fail(self):
        self.inbox.put_nowait(FAIL)

    def stop(self):
        self.inbox.put_nowait(STOP)


class Connector:
    def __init__(self, sockets):
        self.sockets = list(sockets)
        self.calls = 0
        self.urls = []

    async def __call__(self, url, *_args, **_kwargs):
        self.urls.append(url)
        self.calls += 1
        item = self.sockets.pop(0)
        if isinstance(item, type) and issubclass(item, Exception):
            raise item()
        return item


class ScribeStreamTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.chunk_queue = asyncio.Queue()
        self.scribe_events = asyncio.Queue()
        self.events = []
        self.statuses = []
        self.usage = UsageTotals()

    async def _chunks(self):
        while True:
            chunk = await self.chunk_queue.get()
            if chunk is None:
                return
            yield chunk

    def push(self, *chunks):
        for chunk in chunks:
            self.chunk_queue.put_nowait(chunk)

    def end(self):
        self.chunk_queue.put_nowait(None)

    async def settle(self, cycles=20):
        for _ in range(cycles):
            await asyncio.sleep(0)

    def drain_events(self):
        events = []
        while not self.scribe_events.empty():
            events.append(self.scribe_events.get_nowait())
        return events

    def start(self, connector, vad_silence_threshold_secs=None, wake_audio=None, **patches):
        fake_websockets = types.SimpleNamespace(connect=connector)
        defaults = dict(MIC_SCRIBE_GATE_HOLD_SECS=0, SCRIBE_RECONNECT_BASE_SECS=0)
        defaults.update(patches)
        self._patches = [
            mock.patch.dict(sys.modules, {"websockets": fake_websockets}),
            mock.patch.multiple("voice.elevenlabs_io", **defaults),
        ]
        for patch in self._patches:
            patch.start()
        streamer_kwargs = {}
        if vad_silence_threshold_secs is not None:
            streamer_kwargs["vad_silence_threshold_secs"] = vad_silence_threshold_secs
        if wake_audio is not None:
            streamer_kwargs["wake_audio"] = wake_audio
        return asyncio.create_task(
            stream_audio_to_scribe(
                self._chunks(),
                self.scribe_events,
                "key",
                usage=self.usage,
                on_status=self.statuses.append,
                on_event=self.events.append,
                **streamer_kwargs,
            )
        )

    async def finish(self, task):
        self.end()
        await asyncio.wait_for(task, timeout=1.0)
        for patch in self._patches:
            patch.stop()

    def event_types(self):
        return [event["type"] for event in self.events]

    async def test_quiet_emits_activity_but_uploads_nothing(self):
        socket = FakeScribe()
        task = self.start(Connector([socket]))
        self.push(QUIET, QUIET, QUIET)
        await self.settle()
        await self.finish(task)

        self.assertEqual(socket.sent, [])
        self.assertIn("audio_activity", [event["type"] for event in self.drain_events()])

    async def test_preopen_attempts_connect(self):
        connector = Connector([FakeScribe()])
        task = self.start(connector)
        self.push(QUIET)
        await self.settle()
        await self.finish(task)

        self.assertEqual(connector.calls, 1)

    async def test_vad_silence_threshold_secs_appears_in_scribe_url(self):
        connector = Connector([FakeScribe()])
        task = self.start(connector, vad_silence_threshold_secs=1.2)
        self.push(QUIET)
        await self.settle()
        await self.finish(task)

        self.assertIn("vad_silence_threshold_secs=1.2", connector.urls[0])

    async def test_preopen_failure_does_not_stop_streamer(self):
        connector = Connector([RuntimeError, FakeScribe()])
        task = self.start(connector)
        self.push(QUIET)
        await self.settle()
        self.assertFalse(task.done())

        self.push(QUIET, QUIET)
        await self.settle()
        self.assertFalse(task.done())
        await self.finish(task)
        self.assertIn("audio_activity", [event["type"] for event in self.drain_events()])

    async def test_threshold_crossing_opens_scribe(self):
        socket = FakeScribe()
        connector = Connector([socket])
        task = self.start(connector)
        self.push(LOUD)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        await self.finish(task)

        self.assertEqual(connector.calls, 1)
        self.assertTrue(socket.sent)

    async def test_cancel_while_uploading_publishes_closed_state(self):
        socket = FakeScribe()
        task = self.start(Connector([socket]))
        try:
            self.push(LOUD)
            await self.settle()
            self.push(LOUD)
            await self.settle()

            self.assertTrue(any(status.get("scribe_state") == "uploading" for status in self.statuses))

            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        finally:
            for patch in self._patches:
                patch.stop()

        self.assertEqual(self.statuses[-1]["scribe_state"], "closed")

    async def test_sends_preroll_before_live_audio(self):
        socket = FakeScribe()
        task = self.start(Connector([socket]), SCRIBE_PREROLL_SECS=0.1)
        self.push(SOFT, SOFT, SOFT)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        await self.finish(task)

        self.assertEqual(len(socket.sent), 4)
        self.assertEqual(decode_payload(socket.sent[0]), SOFT)
        self.assertEqual(decode_payload(socket.sent[-1]), LOUD)

    async def test_wake_audio_uploaded_before_live_audio(self):
        socket = FakeScribe()
        task = self.start(Connector([socket]), wake_audio=[WAKE], SCRIBE_PREROLL_SECS=0.1)
        self.push(SOFT)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        await self.finish(task)

        self.assertEqual(len(socket.sent), 3)
        self.assertEqual(decode_payload(socket.sent[0]), WAKE)
        self.assertEqual(decode_payload(socket.sent[-1]), LOUD)

    async def test_wake_audio_survives_a_small_preroll_window(self):
        # Preroll window is 2 frames but wake audio is 3: the seeded frames must not
        # be evicted while the socket connects.
        socket = FakeScribe()
        task = self.start(Connector([socket]), wake_audio=[WAKE, WAKE, WAKE], SCRIBE_PREROLL_SECS=0.04)
        self.push(LOUD)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        await self.finish(task)

        self.assertEqual(len(socket.sent), 5)
        self.assertEqual([decode_payload(message) for message in socket.sent[:3]], [WAKE, WAKE, WAKE])

    async def test_wake_audio_uploaded_only_once(self):
        socket = FakeScribe()
        task = self.start(
            Connector([socket]),
            wake_audio=[WAKE],
            SCRIBE_PREROLL_SECS=0.04,
            SCRIBE_POST_SPEECH_TAIL_SECS=0.0,
            SCRIBE_COMMIT_TIMEOUT_SECS=10.0,
            SCRIBE_HOLD_OPEN_SECS=10.0,
        )
        self.push(LOUD)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        self.push(QUIET)
        await self.settle()
        socket.deliver(committed_message("first utterance"))
        await self.settle()
        self.push(QUIET)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        await self.finish(task)

        wake_sends = [message for message in socket.sent if decode_payload(message) == WAKE]
        self.assertEqual(len(wake_sends), 1)

    async def test_usage_counts_only_sent_bytes(self):
        socket = FakeScribe()
        task = self.start(Connector([socket]), SCRIBE_PREROLL_SECS=0.02)
        self.push(QUIET, QUIET, QUIET, QUIET, QUIET)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        await self.finish(task)

        self.assertAlmostEqual(self.usage.stt_audio_seconds, CHUNK_SECS)

    async def test_speech_end_sends_quiet_tail(self):
        socket = FakeScribe()
        task = self.start(Connector([socket]), SCRIBE_POST_SPEECH_TAIL_SECS=0.04)
        self.push(LOUD)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        self.push(QUIET, QUIET, QUIET)
        await self.settle()
        await self.finish(task)

        silent = [message for message in socket.sent if is_silent(message)]
        self.assertGreaterEqual(len(silent), 1)
        self.assertTrue(all(is_silent(message) for message in silent))

    async def test_commit_after_tail_is_forwarded(self):
        socket = FakeScribe()
        task = self.start(
            Connector([socket]),
            SCRIBE_POST_SPEECH_TAIL_SECS=0.04,
            SCRIBE_COMMIT_TIMEOUT_SECS=10.0,
        )
        self.push(LOUD)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        self.push(QUIET)
        await self.settle()
        socket.deliver(committed_message("hello there"))
        await self.settle()
        self.push(QUIET)
        await self.settle()
        await self.finish(task)

        commits = [event for event in self.drain_events() if event["type"] == "commit"]
        self.assertEqual(commits, [{"type": "commit", "text": "hello there"}])

    async def test_commit_before_local_gate_closes_enters_hold_open(self):
        socket = FakeScribe()
        task = self.start(
            Connector([socket]),
            SCRIBE_POST_SPEECH_TAIL_SECS=0.04,
            SCRIBE_COMMIT_TIMEOUT_SECS=0.04,
            SCRIBE_HOLD_OPEN_SECS=10.0,
        )
        self.push(LOUD)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        socket.deliver(committed_message("early commit"))
        await self.settle()
        self.push(QUIET)
        await self.settle()
        sent_after_commit = len(socket.sent)

        self.push(QUIET, QUIET, QUIET)
        await self.settle()
        await self.finish(task)

        self.assertNotIn("scribe_commit_timeout", self.event_types())
        self.assertEqual(len(socket.sent), sent_after_commit)
        commits = [event for event in self.drain_events() if event["type"] == "commit"]
        self.assertEqual(commits, [{"type": "commit", "text": "early commit"}])

    async def test_speech_after_early_commit_still_gets_tail(self):
        socket = FakeScribe()
        task = self.start(
            Connector([socket]),
            SCRIBE_POST_SPEECH_TAIL_SECS=0.04,
            SCRIBE_COMMIT_TIMEOUT_SECS=10.0,
        )
        self.push(LOUD)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        socket.deliver(committed_message("early commit"))
        await self.settle()
        sent_after_commit = len(socket.sent)

        self.push(LOUD)
        await self.settle()
        self.push(QUIET)
        await self.settle()
        await self.finish(task)

        silent = [message for message in socket.sent[sent_after_commit:] if is_silent(message)]
        self.assertGreaterEqual(len(silent), 1)

    async def test_commit_timeout_closes_without_synthetic_commit(self):
        socket = FakeScribe()
        task = self.start(
            Connector([socket]),
            SCRIBE_POST_SPEECH_TAIL_SECS=0.0,
            SCRIBE_COMMIT_TIMEOUT_SECS=0.04,
        )
        self.push(LOUD)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        self.push(QUIET, QUIET, QUIET, QUIET, QUIET)
        await self.settle()
        await self.finish(task)

        self.assertIn("scribe_commit_timeout", self.event_types())
        self.assertTrue(socket.closed)
        self.assertNotIn("commit", [event["type"] for event in self.drain_events()])

    async def test_hold_open_does_not_upload_quiet(self):
        socket = FakeScribe()
        task = self.start(
            Connector([socket]),
            SCRIBE_POST_SPEECH_TAIL_SECS=0.0,
            SCRIBE_COMMIT_TIMEOUT_SECS=10.0,
            SCRIBE_HOLD_OPEN_SECS=10.0,
        )
        self.push(LOUD)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        self.push(QUIET)
        await self.settle()
        socket.deliver(committed_message("done"))
        await self.settle()
        self.push(QUIET)
        await self.settle()
        sent_after_commit = len(socket.sent)

        self.push(QUIET, QUIET, QUIET)
        await self.settle()
        await self.finish(task)

        self.assertEqual(len(socket.sent), sent_after_commit)

    async def test_speech_during_hold_open_uploads_without_new_connect(self):
        socket = FakeScribe()
        connector = Connector([socket])
        task = self.start(
            connector,
            SCRIBE_POST_SPEECH_TAIL_SECS=0.0,
            SCRIBE_COMMIT_TIMEOUT_SECS=10.0,
            SCRIBE_HOLD_OPEN_SECS=10.0,
        )
        self.push(LOUD)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        self.push(QUIET)
        await self.settle()
        socket.deliver(committed_message("done"))
        await self.settle()
        self.push(QUIET)
        await self.settle()
        sent_after_commit = len(socket.sent)

        self.push(LOUD)
        await self.settle()
        await self.finish(task)

        self.assertEqual(connector.calls, 1)
        self.assertGreater(len(socket.sent), sent_after_commit)

    async def test_idle_socket_close_is_normal(self):
        socket = FakeScribe()
        connector = Connector([socket])
        task = self.start(connector)
        self.push(QUIET)
        await self.settle()
        socket.stop()
        await self.settle()
        self.push(QUIET, QUIET)
        await self.settle()
        await self.finish(task)

        self.assertTrue(socket.closed)
        self.assertEqual(connector.calls, 1)
        self.assertNotIn("scribe_reconnect", self.event_types())

    async def test_successful_open_clears_previous_scribe_error(self):
        connector = Connector([RuntimeError, FakeScribe()])
        task = self.start(connector, SCRIBE_RECONNECT_BASE_SECS=0)
        self.push(QUIET)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        await self.finish(task)

        errors = [status.get("scribe_last_error") for status in self.statuses if "scribe_last_error" in status]
        self.assertIn("", [error or "" for error in errors])
        self.assertIsNone(self.statuses[-1]["scribe_last_error"])

    async def test_midspeech_failure_reconnects_while_speech_active(self):
        first = FakeScribe()
        second = FakeScribe()
        connector = Connector([first, second])
        task = self.start(connector)
        self.push(LOUD)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        self.assertTrue(first.sent)

        first.fail()
        await self.settle()
        self.push(LOUD)
        await self.settle()
        self.push(LOUD)
        await self.settle()
        second.deliver(committed_message("continued"))
        await self.settle()
        await self.finish(task)

        self.assertEqual(connector.calls, 2)
        self.assertIn("scribe_reconnect", self.event_types())
        self.assertTrue(second.sent)
        commits = [event for event in self.drain_events() if event["type"] == "commit"]
        self.assertEqual(commits, [{"type": "commit", "text": "continued"}])


if __name__ == "__main__":
    unittest.main()
