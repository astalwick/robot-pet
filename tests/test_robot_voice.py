import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from robot_voice import RobotVoiceService, TimelineBuffer
from config.voice import load_voice_config


class RobotVoiceServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_config_does_not_start_orchestrator(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "voice.json")
            with open(config_path, "w") as file_obj:
                json.dump({"enabled": False}, file_obj)

            service = RobotVoiceService(
                config_path,
                "/tmp/missing.sock",
                command_socket=os.path.join(tmpdir, "cmd.sock"),
                poll_seconds=0.01,
            )
            stop_event = asyncio.Event()

            async def stop_soon():
                await asyncio.sleep(0.03)
                stop_event.set()

            with mock.patch.object(service, "start_orchestrator", new=mock.AsyncMock()) as start_orchestrator:
                await asyncio.gather(service.run(stop_event), stop_soon())

        start_orchestrator.assert_not_called()

    async def test_wake_orchestrator_starts_without_api_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "voice.json")
            with open(config_path, "w") as file_obj:
                json.dump({"enabled": True, "wake_word_enabled": True}, file_obj)

            service = RobotVoiceService(
                config_path,
                "/tmp/missing.sock",
                command_socket=os.path.join(tmpdir, "cmd.sock"),
                poll_seconds=0.01,
            )
            stop_event = asyncio.Event()

            async def stop_soon():
                await asyncio.sleep(0.05)
                stop_event.set()

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(service, "start_orchestrator", new=mock.AsyncMock()) as start_orchestrator:
                    await asyncio.gather(service.run(stop_event), stop_soon())

            start_orchestrator.assert_called()

    async def test_orchestrator_failure_restarts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "voice.json")
            with open(config_path, "w") as file_obj:
                json.dump({"enabled": True, "wake_word_enabled": True}, file_obj)

            service = RobotVoiceService(
                config_path,
                "/tmp/missing.sock",
                command_socket=os.path.join(tmpdir, "cmd.sock"),
                poll_seconds=0.01,
            )
            stop_event = asyncio.Event()
            published = []
            start_count = 0

            async def fake_start_orchestrator(config):
                nonlocal start_count
                start_count += 1
                if start_count == 1:

                    async def fail():
                        raise RuntimeError("orchestrator failed")

                    service._orchestrator_task = asyncio.create_task(fail())
                    service.active_config = config
                    await asyncio.sleep(0)

            async def stop_soon():
                await asyncio.sleep(0.08)
                stop_event.set()

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("robot_voice.publish_message", side_effect=lambda _socket, message: published.append(message) or True):
                    with mock.patch.object(service, "start_orchestrator", side_effect=fake_start_orchestrator):
                        await asyncio.gather(service.run(stop_event), stop_soon())

            self.assertGreaterEqual(start_count, 2)
            self.assertTrue(any(message.get("status") == "reconnecting" for message in published))

    async def test_start_orchestrator_rejects_missing_chime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "voice.json")
            with open(config_path, "w") as file_obj:
                json.dump(
                    {
                        "enabled": True,
                        "wake_word_enabled": True,
                        "wake_chime_path": os.path.join(tmpdir, "missing.wav"),
                    },
                    file_obj,
                )

            service = RobotVoiceService(
                config_path,
                "/tmp/missing.sock",
                command_socket=os.path.join(tmpdir, "cmd.sock"),
                poll_seconds=0.01,
            )
            published = []

            with mock.patch("robot_voice.publish_message", side_effect=lambda _socket, message: published.append(message) or True):
                await service.start_orchestrator(load_voice_config(config_path))

            self.assertIsNone(service._orchestrator_task)
            self.assertTrue(any("Chime WAV not found" in str(message.get("last_error")) for message in published))

    async def test_enabled_without_wake_waits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "voice.json")
            with open(config_path, "w") as file_obj:
                json.dump({"enabled": True}, file_obj)

            service = RobotVoiceService(
                config_path,
                "/tmp/missing.sock",
                command_socket=os.path.join(tmpdir, "cmd.sock"),
                poll_seconds=0.01,
            )
            stop_event = asyncio.Event()
            published = []

            async def stop_soon():
                await asyncio.sleep(0.03)
                stop_event.set()

            with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "x", "OPENAI_API_KEY": "y"}):
                with mock.patch("robot_voice.publish_message", side_effect=lambda _socket, message: published.append(message) or True):
                    with mock.patch.object(service, "start_orchestrator", new=mock.AsyncMock()) as start_orchestrator:
                        await asyncio.gather(service.run(stop_event), stop_soon())

            start_orchestrator.assert_not_called()
            self.assertTrue(any(message.get("status") == "waiting" for message in published))

    async def test_voice_errors_are_logged_once(self):
        service = RobotVoiceService("/tmp/missing.json", "/tmp/missing.sock", poll_seconds=0.01)

        with mock.patch("robot_voice.log") as log:
            service.publish(service_config(), status="error", last_error="bad output")
            service.publish(service_config(), status="error", last_error="bad output")
            service.publish(service_config(), status="listening", last_error=None)
            service.publish(service_config(), status="error", last_error="bad output")

        self.assertEqual(log.error.call_count, 2)


class TimelineBufferTest(unittest.TestCase):
    def test_partial_flood_does_not_evict_signal_events(self):
        buffer = TimelineBuffer()
        buffer.add_event({"type": "barge_in_fired", "t": 1.0, "reason": "user_spoke"})
        for index in range(2000):
            buffer.add_event({"type": "partial", "t": 1.5 + index * 0.001, "text": "..."})

        snapshot = buffer.snapshot(now=2.0)
        kinds = [event["type"] for event in snapshot["events"]]
        self.assertIn("barge_in_fired", kinds)

    def test_trim_drops_events_older_than_horizon(self):
        buffer = TimelineBuffer()
        buffer.add_event({"type": "barge_in_fired", "t": 0.0})
        buffer.add_event({"type": "state", "t": 0.0, "state": "listening"})
        buffer.add_event({"type": "partial", "t": 0.0, "text": "old"})
        buffer.add_event({"type": "barge_in_fired", "t": 100.0})

        buffer.trim(now=120.0)
        kinds = [event["type"] for event in buffer.snapshot(now=120.0)["events"]]
        self.assertEqual(kinds, ["barge_in_fired"])

    def test_snapshot_events_are_time_ordered(self):
        buffer = TimelineBuffer()
        buffer.add_event({"type": "state", "t": 1.0, "state": "listening"})
        buffer.add_event({"type": "partial", "t": 0.5, "text": "a"})
        buffer.add_event({"type": "barge_in_fired", "t": 2.0})
        buffer.add_event({"type": "partial", "t": 1.5, "text": "b"})

        times = [event["t"] for event in buffer.snapshot(now=3.0)["events"]]
        self.assertEqual(times, sorted(times))


def service_config():
    from config.voice import VoiceConfig

    return VoiceConfig()


if __name__ == "__main__":
    unittest.main()
