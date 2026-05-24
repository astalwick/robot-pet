import asyncio
import os
import sys
import time
import unittest
from contextlib import suppress
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from robot_voice import RobotVoiceService
from config.voice import VoiceConfig


class RobotVoiceWakeTest(unittest.IsolatedAsyncioTestCase):
    def test_armed_mode_has_no_session(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service._mode = "armed"
        self.assertIsNone(service.session)

    async def test_wait_for_idle_after_commit(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service.active_config = VoiceConfig(session_idle_secs=0.1)
        service._mode = "active"
        service._last_commit_at = time.monotonic() - 1.0
        service.status["status"] = "listening"
        service.status["assistant_speaking"] = False

        await asyncio.wait_for(service._wait_for_idle(), timeout=1.0)

    async def test_wait_for_idle_ignored_while_speaking(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service.active_config = VoiceConfig(session_idle_secs=0.05)
        service._mode = "active"
        service._last_commit_at = time.monotonic() - 1.0
        service.status["status"] = "listening"
        service.status["assistant_speaking"] = True

        idle_task = asyncio.create_task(service._wait_for_idle())
        await asyncio.sleep(0.15)
        self.assertFalse(idle_task.done())
        idle_task.cancel()
        with suppress(asyncio.CancelledError):
            await idle_task

    async def test_publish_commit_updates_idle_clock(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        config = VoiceConfig()
        with mock.patch("robot_voice.publish_message", return_value=True):
            service.publish(config, last_committed_transcript="first")
        first = service._last_commit_at
        self.assertIsNotNone(first)
        await asyncio.sleep(0.02)
        with mock.patch("robot_voice.publish_message", return_value=True):
            service.publish(config, last_committed_transcript="second")
        self.assertGreater(service._last_commit_at, first)

    async def test_deactivate_clears_history(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")
        service.active_config = VoiceConfig()
        service._mode = "active"
        session = mock.Mock()
        session.history = mock.Mock()
        session.stop = mock.AsyncMock()
        service.session = session

        await service._deactivate_session()

        session.history.clear.assert_called_once()
        self.assertIsNone(service.session)
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


if __name__ == "__main__":
    unittest.main()
