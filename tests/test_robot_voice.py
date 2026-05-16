import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from robot_voice import RobotVoiceService


class RobotVoiceServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_config_does_not_start_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "voice.json")
            with open(config_path, "w") as file_obj:
                json.dump({"enabled": False}, file_obj)

            service = RobotVoiceService(config_path, "/tmp/missing.sock", poll_seconds=0.01)
            stop_event = asyncio.Event()

            async def stop_soon():
                await asyncio.sleep(0.03)
                stop_event.set()

            with mock.patch.object(service, "start_session") as start_session:
                await asyncio.gather(service.run(stop_event), stop_soon())

        start_session.assert_not_called()

    async def test_missing_api_keys_publishes_error_and_does_not_start_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "voice.json")
            with open(config_path, "w") as file_obj:
                json.dump({"enabled": True}, file_obj)

            service = RobotVoiceService(config_path, "/tmp/missing.sock", poll_seconds=0.01)
            stop_event = asyncio.Event()
            published = []

            async def stop_soon():
                await asyncio.sleep(0.03)
                stop_event.set()

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("robot_voice.publish_message", side_effect=lambda _socket, message: published.append(message) or True):
                    with mock.patch.object(service, "start_session") as start_session:
                        await asyncio.gather(service.run(stop_event), stop_soon())

        start_session.assert_not_called()
        self.assertTrue(any(message["status"] == "error" for message in published))
        self.assertTrue(any("ELEVENLABS_API_KEY" in message["last_error"] for message in published))


if __name__ == "__main__":
    unittest.main()
