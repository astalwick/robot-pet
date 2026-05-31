import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from robot_telemetry import TelemetryHub, parse_meminfo, sample_pi_health
from telemetry.messages import (
    decode_json_line,
    gamepad_teleop_update,
    motor_rail_update,
    sensors_update,
    vision_update,
    voice_update,
)
from telemetry.socket_client import publish_message


class PiHealthTest(unittest.TestCase):
    def test_parse_meminfo_returns_used_and_total_mb(self):
        meminfo = "MemTotal:        4096000 kB\nMemAvailable:    1024000 kB\n"

        self.assertEqual(parse_meminfo(meminfo), (3000, 4000))

    def test_sample_pi_health_uses_injected_readers(self):
        def read_file(path):
            return {
                "/proc/uptime": "1234.56 100.0\n",
                "/proc/loadavg": "0.22 0.30 0.40 1/100 123\n",
                "/proc/meminfo": "MemTotal:        4096000 kB\nMemAvailable:    1024000 kB\n",
                "/sys/class/thermal/thermal_zone0/temp": "48500\n",
            }.get(path)

        def command_runner(command):
            if command == ["vcgencmd", "get_throttled"]:
                return "throttled=0x0"
            return None

        health = sample_pi_health(
            read_file=read_file,
            disk_usage=lambda _path: SimpleNamespace(total=100, used=18, free=82),
            command_runner=command_runner,
        )

        self.assertEqual(health["uptime_seconds"], 1234)
        self.assertEqual(health["load_1m"], 0.22)
        self.assertEqual(health["memory_used_mb"], 3000)
        self.assertEqual(health["disk_used_percent"], 18.0)
        self.assertEqual(health["soc_temp_c"], 48.5)
        self.assertEqual(health["throttled_flags"], "0x0")
        self.assertIsNone(health["power_bank_charge"])


class TelemetryHubTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.publish_socket = os.path.join(self.tmpdir.name, "pub.sock")
        self.subscribe_socket = os.path.join(self.tmpdir.name, "sub.sock")
        self.hub = TelemetryHub(
            publish_socket=self.publish_socket,
            subscribe_socket=self.subscribe_socket,
            rate_hz=20.0,
            stale_timeout=1.0,
            sampler=lambda: {"uptime_seconds": 1, "power_bank_charge": None},
        )
        await self.hub.start()

    async def asyncTearDown(self):
        await self.hub.stop()
        self.tmpdir.cleanup()

    async def test_publisher_update_appears_in_subscriber_snapshot(self):
        reader, writer = await asyncio.open_unix_connection(self.subscribe_socket)
        await reader.readline()

        message = gamepad_teleop_update(
            controller={"connected": True},
            wheels={"left_target_qpps": 100},
            motor_battery={"pack_voltage": 11.7, "cell_voltage": 3.9, "status": "ok"},
            drive_tuning={"speed_scale": 0.25},
            drive_status={"state": "driving"},
        )

        self.assertTrue(publish_message(self.publish_socket, message))
        snapshot = await self._read_until(reader, lambda item: item["controller"] == {"connected": True})

        writer.close()
        await writer.wait_closed()
        self.assertEqual(snapshot["wheels"]["left_target_qpps"], 100)
        self.assertEqual(snapshot["drive_tuning"]["speed_scale"], 0.25)
        self.assertEqual(snapshot["drive_status"]["state"], "driving")
        self.assertFalse(snapshot["sources"]["gamepad_teleop"]["stale"])

    async def test_source_is_marked_stale_after_timeout(self):
        self.hub.stale_timeout = 0.05
        self.assertTrue(
            publish_message(
                self.publish_socket,
                gamepad_teleop_update({}, {}, {"pack_voltage": None, "cell_voltage": None, "status": "unknown"}),
            )
        )
        await asyncio.sleep(0.08)

        snapshot = self.hub.build_snapshot()

        self.assertTrue(snapshot["sources"]["gamepad_teleop"]["stale"])

    async def test_snapshot_lists_vision_source_as_stale_before_any_update(self):
        snapshot = self.hub.build_snapshot()

        self.assertIn("vision", snapshot["sources"])
        self.assertTrue(snapshot["sources"]["vision"]["stale"])
        self.assertIsNone(snapshot["vision"])

    async def test_snapshot_lists_sensors_source_as_stale_before_any_update(self):
        snapshot = self.hub.build_snapshot()

        self.assertIn("sensors", snapshot["sources"])
        self.assertTrue(snapshot["sources"]["sensors"]["stale"])
        self.assertIsNone(snapshot["sensors"])

    async def test_snapshot_lists_motor_rail_source_as_stale_before_any_update(self):
        snapshot = self.hub.build_snapshot()

        self.assertIn("motor_rail", snapshot["sources"])
        self.assertTrue(snapshot["sources"]["motor_rail"]["stale"])
        self.assertIsNone(snapshot["motor_rail"])

    async def test_snapshot_lists_voice_source_as_stale_before_any_update(self):
        snapshot = self.hub.build_snapshot()

        self.assertIn("voice", snapshot["sources"])
        self.assertTrue(snapshot["sources"]["voice"]["stale"])
        self.assertIsNone(snapshot["voice"])

    async def test_vision_update_appears_in_subscriber_snapshot(self):
        reader, writer = await asyncio.open_unix_connection(self.subscribe_socket)
        await reader.readline()

        message = vision_update(
            enabled=True,
            status="detecting",
            faces=[{"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}],
            image_width=1280,
            image_height=720,
            detection_rate_hz=2.0,
            last_detection_time=1234.5,
        )

        self.assertTrue(publish_message(self.publish_socket, message))
        snapshot = await self._read_until(reader, lambda item: item.get("vision") is not None)

        writer.close()
        await writer.wait_closed()
        self.assertEqual(snapshot["vision"]["status"], "detecting")
        self.assertEqual(snapshot["vision"]["faces"][0]["width"], 0.3)
        self.assertEqual(snapshot["vision"]["image_width"], 1280)
        self.assertFalse(snapshot["sources"]["vision"]["stale"])

    async def test_sensors_update_appears_in_subscriber_snapshot(self):
        reader, writer = await asyncio.open_unix_connection(self.subscribe_socket)
        await reader.readline()

        message = sensors_update(
            enabled=True,
            status="polling",
            readings=[
                {
                    "name": "cliff_left",
                    "kind": "vl53l0x",
                    "channel": 0,
                    "distance_mm": 88,
                    "ok": True,
                }
            ],
            poll_rate_hz=10.0,
        )

        self.assertTrue(publish_message(self.publish_socket, message))
        snapshot = await self._read_until(reader, lambda item: item.get("sensors") is not None)

        writer.close()
        await writer.wait_closed()
        self.assertEqual(snapshot["sensors"]["readings"][0]["distance_mm"], 88)
        self.assertFalse(snapshot["sources"]["sensors"]["stale"])

    async def test_voice_update_appears_in_subscriber_snapshot(self):
        reader, writer = await asyncio.open_unix_connection(self.subscribe_socket)
        await reader.readline()

        message = voice_update(
            enabled=True,
            status="listening",
            input_device="hw:0,0",
            output_device="plughw:0,0",
            sample_rate=16000,
            capture_channels=6,
            capture_channel_index=1,
        )

        self.assertTrue(publish_message(self.publish_socket, message))
        snapshot = await self._read_until(reader, lambda item: item.get("voice") is not None)

        writer.close()
        await writer.wait_closed()
        self.assertEqual(snapshot["voice"]["status"], "listening")
        self.assertEqual(snapshot["voice"]["capture_channel_index"], 1)
        self.assertFalse(snapshot["sources"]["voice"]["stale"])

    async def test_motor_rail_update_appears_in_subscriber_snapshot(self):
        reader, writer = await asyncio.open_unix_connection(self.subscribe_socket)
        await reader.readline()

        self.assertTrue(
            publish_message(
                self.publish_socket,
                motor_rail_update("on", 24, 11.8, "startup", 10.8, 11.1),
            )
        )
        snapshot = await self._read_until(reader, lambda item: item.get("motor_rail") is not None)

        writer.close()
        await writer.wait_closed()
        self.assertEqual(snapshot["motor_rail"]["state"], "on")
        self.assertEqual(snapshot["motor_rail"]["mosfet_gpio"], 24)
        self.assertFalse(snapshot["sources"]["motor_rail"]["stale"])

    async def test_hub_keeps_running_after_subscriber_disconnects(self):
        reader, writer = await asyncio.open_unix_connection(self.subscribe_socket)
        await reader.readline()
        writer.close()
        await writer.wait_closed()

        await self.hub.broadcast(self.hub.build_snapshot())

        self.assertTrue(self.hub._servers)

    async def _read_until(self, reader, predicate):
        deadline = asyncio.get_running_loop().time() + 1.0
        while asyncio.get_running_loop().time() < deadline:
            line = await asyncio.wait_for(reader.readline(), timeout=1.0)
            snapshot = decode_json_line(line)
            if predicate(snapshot):
                return snapshot
        self.fail("timed out waiting for telemetry snapshot")


if __name__ == "__main__":
    unittest.main()
