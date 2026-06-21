import asyncio
import os
import sys
import tempfile
import time
import unittest
from contextlib import suppress
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from robot_voice import ACTIVATE_FAILURE_BACKOFF_SECS, RobotVoiceService
from config.voice import VoiceConfig
from telemetry.socket_client import send_voice_command


class RobotVoiceWakeTest(unittest.IsolatedAsyncioTestCase):
    def test_armed_mode_has_no_session(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service._mode = "armed"
        self.assertIsNone(service.session)

    async def test_wait_for_idle_after_quiet_time(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service.active_config = VoiceConfig(wake_word_enabled=True, session_idle_secs=0.1)
        service._mode = "active"
        service._idle_started_at = time.monotonic() - 1.0
        service.status["status"] = "listening"
        service.status["assistant_speaking"] = False

        await asyncio.wait_for(service._wait_for_idle(), timeout=1.0)

    async def test_wait_for_idle_after_quiet_time_when_status_sticks_thinking(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service.active_config = VoiceConfig(wake_word_enabled=True, session_idle_secs=0.1)
        service._mode = "active"
        service._idle_started_at = time.monotonic() - 1.0
        service.status["status"] = "thinking"
        service.status["assistant_speaking"] = False

        await asyncio.wait_for(service._wait_for_idle(), timeout=1.0)

    async def test_wait_for_idle_ignored_while_speaking(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service.active_config = VoiceConfig(wake_word_enabled=True, session_idle_secs=0.05)
        service._mode = "active"
        service._idle_started_at = time.monotonic() - 1.0
        service.status["status"] = "listening"
        service.status["assistant_speaking"] = True

        idle_task = asyncio.create_task(service._wait_for_idle())
        await asyncio.sleep(0.15)
        self.assertFalse(idle_task.done())
        idle_task.cancel()
        with suppress(asyncio.CancelledError):
            await idle_task

    async def test_wait_for_idle_ignored_while_assistant_working(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service.active_config = VoiceConfig(wake_word_enabled=True, session_idle_secs=0.05)
        service._mode = "active"
        service._idle_started_at = time.monotonic() - 1.0
        service.status["status"] = "thinking"
        service.status["assistant_speaking"] = False
        service.status["assistant_working"] = True

        idle_task = asyncio.create_task(service._wait_for_idle())
        await asyncio.sleep(0.15)
        self.assertFalse(idle_task.done())
        service.status["status"] = "listening"
        service.status["assistant_working"] = False
        await asyncio.wait_for(idle_task, timeout=1.0)

    async def test_publish_listening_updates_idle_clock(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service._mode = "active"
        service.status["status"] = "speaking"
        service.status["assistant_speaking"] = True
        config = VoiceConfig()
        with mock.patch("robot_voice.publish_message", return_value=True):
            service.publish(config, status="listening", assistant_speaking=False)
        first = service._idle_started_at
        self.assertIsNotNone(first)
        await asyncio.sleep(0.02)
        with mock.patch("robot_voice.publish_message", return_value=True):
            service.publish(config, status="speaking", assistant_speaking=True)
            service.publish(config, status="listening", assistant_speaking=False)
        self.assertGreater(service._idle_started_at, first)

    async def test_deactivate_clears_history(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service.active_config = VoiceConfig(wake_word_enabled=False)
        service._mode = "active"
        session = mock.Mock()
        session.history = mock.Mock()
        session.stop = mock.AsyncMock()
        service.session = session

        published = []
        service.timeline.add_event({"type": "phase", "t": time.monotonic() - 1.0, "name": "thinking", "on": True})
        service._last_timeline_publish_at = time.monotonic()

        with mock.patch("robot_voice.publish_message", side_effect=lambda _socket, message: published.append(message) or True):
            await service._deactivate_session()

        session.history.clear.assert_called_once()
        self.assertIsNone(service.session)
        self.assertEqual(service._mode, "armed")
        self.assertIn("timeline", published[-1])
        phase_events = [
            event
            for event in published[-1]["timeline"]["events"]
            if event.get("type") == "phase" and event.get("name") == "thinking"
        ]
        self.assertFalse(phase_events[-1]["on"])

        service.publish(service.active_config, status="waiting", assistant_speaking=False)
        self.assertIn("timeline", published[-1])

    async def test_deactivate_resets_detector_before_arming(self):
        # The detector is not fed during a session, so its streaming buffer holds
        # stale pre-session audio. Re-arming must reset it, or that stale audio
        # plus the end-chime tail fires a spurious wake right at session end.
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service.active_config = VoiceConfig(wake_word_enabled=False)
        service._mode = "active"
        service._detector = mock.Mock()

        with mock.patch("robot_voice.publish_message", return_value=True):
            await service._deactivate_session()

        service._detector.reset.assert_called_once()
        self.assertEqual(service._mode, "armed")

    async def test_armed_mode_suppresses_wake_scoring(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service._mode = "active"
        service._detector = mock.Mock()
        service._detector.check.return_value = True
        service._io_stop_event = asyncio.Event()

        async def fake_mic_frames(stop_event, queue_size=10, warn_on_drop=False):
            yield b"\x00" * 2560
            await asyncio.sleep(3600)

        service.audio = mock.Mock()
        service.audio.mic_frames = fake_mic_frames

        loop_task = asyncio.create_task(service._run_wake_loop(VoiceConfig()))
        await asyncio.sleep(0.05)
        loop_task.cancel()
        with suppress(asyncio.CancelledError):
            await loop_task
        service._detector.check.assert_not_called()


    async def test_end_session_command_sets_end_event(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service._mode = "active"
        self.assertFalse(service._end_session_event.is_set())
        service.request_end_session()
        self.assertTrue(service._end_session_event.is_set())

    async def test_end_session_command_ignored_when_armed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            command_socket = os.path.join(tmpdir, "cmd.sock")
            service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock", command_socket=command_socket)
            service._mode = "armed"
            server = await service._start_command_server()
            self.assertIsNotNone(server)
            try:
                ack = await asyncio.to_thread(send_voice_command, command_socket, {"cmd": "end_session"})
                self.assertEqual(ack, {"ok": True, "accepted": False, "reason": "no_active_session"})
                self.assertFalse(service._end_session_event.is_set())
            finally:
                server.close()
                with suppress(Exception):
                    await server.wait_closed()

    async def test_end_session_socket_command_sets_event(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            command_socket = os.path.join(tmpdir, "cmd.sock")
            service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock", command_socket=command_socket)
            service._mode = "active"
            server = await service._start_command_server()
            self.assertIsNotNone(server)
            try:
                ack = await asyncio.to_thread(send_voice_command, command_socket, {"cmd": "end_session"})
                self.assertEqual(ack, {"ok": True, "accepted": True, "reason": None})
                await asyncio.wait_for(service._end_session_event.wait(), timeout=1.0)
            finally:
                server.close()
                with suppress(Exception):
                    await server.wait_closed()

    async def test_talk_now_command_sets_wake_event(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            command_socket = os.path.join(tmpdir, "cmd.sock")
            service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock", command_socket=command_socket)
            service._mode = "armed"
            server = await service._start_command_server()
            self.assertIsNotNone(server)
            try:
                self.assertFalse(service._wake_event.is_set())
                ack = await asyncio.to_thread(send_voice_command, command_socket, {"cmd": "talk_now"})
                self.assertEqual(ack, {"ok": True, "accepted": True, "reason": None})
                await asyncio.wait_for(service._wake_event.wait(), timeout=1.0)
            finally:
                server.close()
                with suppress(Exception):
                    await server.wait_closed()

    async def test_unknown_command_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            command_socket = os.path.join(tmpdir, "cmd.sock")
            service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock", command_socket=command_socket)
            server = await service._start_command_server()
            self.assertIsNotNone(server)
            try:
                ack = await asyncio.to_thread(send_voice_command, command_socket, {"cmd": "do_a_barrel_roll"})
                self.assertEqual(ack, {"ok": True, "accepted": False, "reason": "unknown_cmd"})
                self.assertFalse(service._wake_event.is_set())
            finally:
                server.close()
                with suppress(Exception):
                    await server.wait_closed()

    async def test_wake_loop_stays_armed_without_credentials(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service._mode = "armed"
        service._detector = mock.Mock()
        service._detector.check.return_value = True
        service._detector.last_score = 0.9
        service._detector.fire_count = 1
        service._detector.last_fire_at = 0.0
        service._io_stop_event = asyncio.Event()

        async def fake_mic_frames(stop_event, queue_size=10, warn_on_drop=False):
            yield b"\x00" * 2560
            await asyncio.sleep(3600)

        audio = mock.Mock()
        audio.mic_frames = fake_mic_frames
        audio.play_wav = mock.AsyncMock()
        service.audio = audio

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("robot_voice.publish_message", return_value=True):
                loop_task = asyncio.create_task(service._run_wake_loop(VoiceConfig()))
                await asyncio.sleep(0.05)
                loop_task.cancel()
                with suppress(asyncio.CancelledError):
                    await loop_task

        audio.play_wav.assert_awaited()
        self.assertFalse(service._wake_event.is_set())

    async def test_wake_activates_before_chime_completes(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service._mode = "armed"
        service._detector = mock.Mock()
        service._detector.check.return_value = True
        service._detector.last_score = 0.9
        service._detector.fire_count = 1
        service._detector.last_fire_at = 0.0
        service._io_stop_event = asyncio.Event()

        chime_started = asyncio.Event()
        chime_release = asyncio.Event()

        async def fake_mic_frames(stop_event, queue_size=10, warn_on_drop=False):
            yield b"\x00" * 2560
            await asyncio.sleep(3600)

        async def slow_play(_path):
            chime_started.set()
            await chime_release.wait()

        audio = mock.Mock()
        audio.mic_frames = fake_mic_frames
        audio.play_wav = slow_play
        service.audio = audio

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "x", "ELEVENLABS_API_KEY": "y"}):
            with mock.patch("robot_voice.publish_message", return_value=True):
                loop_task = asyncio.create_task(service._run_wake_loop(VoiceConfig()))
                await asyncio.wait_for(chime_started.wait(), timeout=1.0)
                # The session can come up (and Scribe pre-open) while the chime is still playing.
                self.assertTrue(service._wake_event.is_set())
                chime_release.set()
                loop_task.cancel()
                with suppress(asyncio.CancelledError):
                    await loop_task

    async def test_credentialed_wake_chime_failure_is_published(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service._mode = "armed"
        service._detector = mock.Mock()
        service._detector.check.return_value = True
        service._detector.last_score = 0.9
        service._detector.fire_count = 1
        service._detector.last_fire_at = 0.0
        service._io_stop_event = asyncio.Event()

        async def fake_mic_frames(stop_event, queue_size=10, warn_on_drop=False):
            yield b"\x00" * 2560
            await asyncio.sleep(3600)

        async def failed_play(_path):
            raise RuntimeError("chime failed")

        audio = mock.Mock()
        audio.mic_frames = fake_mic_frames
        audio.play_wav = failed_play
        service.audio = audio

        published = []
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "x", "ELEVENLABS_API_KEY": "y"}):
            with mock.patch("robot_voice.publish_message", side_effect=lambda _socket, message: published.append(message) or True):
                loop_task = asyncio.create_task(service._run_wake_loop(VoiceConfig()))
                await asyncio.sleep(0.05)
                loop_task.cancel()
                with suppress(asyncio.CancelledError):
                    await loop_task

        self.assertTrue(service._wake_event.is_set())
        self.assertTrue(any(message.get("last_error") == "chime failed" for message in published))

    async def test_activation_failure_sleeps_before_retry(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service.active_config = VoiceConfig(wake_word_enabled=True)
        service._io_stop_event = asyncio.Event()

        attempts = 0

        async def fake_activate():
            nonlocal attempts
            attempts += 1
            if attempts >= 2:
                service._io_stop_event.set()
            return False

        async def fake_trigger():
            return

        sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def tracking_sleep(seconds):
            sleeps.append(seconds)
            await real_sleep(0)

        with mock.patch("robot_voice.publish_message", return_value=True):
            with mock.patch.object(service, "_activate_session", side_effect=fake_activate):
                with mock.patch.object(service, "_wait_for_session_trigger", side_effect=fake_trigger):
                    with mock.patch("robot_voice.asyncio.sleep", side_effect=tracking_sleep):
                        await asyncio.wait_for(service._run_orchestrator(), timeout=1.0)

        self.assertGreaterEqual(attempts, 2)
        self.assertIn(ACTIVATE_FAILURE_BACKOFF_SECS, sleeps)


if __name__ == "__main__":
    unittest.main()
