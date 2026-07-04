import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))


ROBOT_WEB_DASHBOARD_IMPORT_ERROR = None
try:
    from aiohttp.test_utils import TestClient, TestServer

    from robot_web_dashboard import (
        LOG_COMMAND,
        STATIC_DIR,
        SnapshotStore,
        WebDashboardState,
        build_app,
        format_sse_event,
    )
except ModuleNotFoundError as exc:
    if exc.name != "aiohttp":
        raise
    ROBOT_WEB_DASHBOARD_IMPORT_ERROR = exc


@unittest.skipIf(
    ROBOT_WEB_DASHBOARD_IMPORT_ERROR is not None, "aiohttp is not installed"
)
class SnapshotStoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_latest_is_none_before_any_publish(self):
        store = SnapshotStore(asyncio.get_running_loop())

        self.assertIsNone(store.latest())

    async def test_latest_returns_most_recent_published_snapshot(self):
        store = SnapshotStore(asyncio.get_running_loop())

        store.publish({"seq": 1})
        self.assertEqual(store.latest(), {"seq": 1})

        store.publish({"seq": 2})
        self.assertEqual(store.latest(), {"seq": 2})

    async def test_wait_for_next_returns_snapshot_published_after_wait(self):
        store = SnapshotStore(asyncio.get_running_loop())
        store.publish({"seq": "stale"})

        async def publish_after_delay():
            await asyncio.sleep(0.01)
            store.publish({"seq": "fresh"})

        publisher = asyncio.create_task(publish_after_delay())
        try:
            result = await asyncio.wait_for(store.wait_for_next(), timeout=1.0)
        finally:
            await publisher

        self.assertEqual(result, {"seq": "fresh"})


@unittest.skipIf(
    ROBOT_WEB_DASHBOARD_IMPORT_ERROR is not None, "aiohttp is not installed"
)
class FormatSseEventTest(unittest.TestCase):
    def test_format_sse_event_emits_data_line_and_blank_terminator(self):
        self.assertEqual(
            format_sse_event({"hello": "world"}),
            b'data: {"hello":"world"}\n\n',
        )

    def test_format_sse_event_serializes_compact_json(self):
        self.assertEqual(
            format_sse_event({"a": 1, "b": [2, 3]}),
            b'data: {"a":1,"b":[2,3]}\n\n',
        )


@unittest.skipIf(
    ROBOT_WEB_DASHBOARD_IMPORT_ERROR is not None, "aiohttp is not installed"
)
class WebDashboardHandlersTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.drive_tuning_config_path = os.path.join(self.tmpdir.name, "drive_tuning.json")
        self.vision_config_path = os.path.join(self.tmpdir.name, "vision.json")
        self.voice_config_path = os.path.join(self.tmpdir.name, "voice.json")
        self.sensors_config_path = os.path.join(self.tmpdir.name, "sensors.json")
        self.voice_command_socket = os.path.join(self.tmpdir.name, "voice-command.sock")
        self.redeploy_status_path = os.path.join(self.tmpdir.name, "redeploy-status.json")
        self.store = SnapshotStore(asyncio.get_running_loop())
        self.state = WebDashboardState(
            asyncio.get_running_loop(),
            self.store,
            STATIC_DIR,
            self.drive_tuning_config_path,
            self.vision_config_path,
            self.voice_config_path,
            self.voice_command_socket,
            self.sensors_config_path,
            self.redeploy_status_path,
        )
        self.client = TestClient(TestServer(build_app(self.state)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmpdir.cleanup()

    async def test_index_serves_dashboard_html(self):
        async with self.client.get("/") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("no-store", resp.headers["Cache-Control"])
            body = await resp.text()

        self.assertIn("Robo-Pet Dashboard", body)
        self.assertIn("/static/dashboard.css", body)
        self.assertIn("/static/main.js", body)
        self.assertIn("IMU Orientation", body)

    async def test_static_main_js_is_served(self):
        async with self.client.get("/static/main.js") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("no-store", resp.headers["Cache-Control"])
            body = await resp.text()

        self.assertIn("connectTelemetry", body)

    async def test_static_telemetry_js_is_served(self):
        async with self.client.get("/static/telemetry.js") as resp:
            self.assertEqual(resp.status, 200)
            body = await resp.text()

        self.assertIn("EventSource", body)
        self.assertIn("imu.yaw_degrees", body)

    async def test_static_path_history_js_is_served(self):
        async with self.client.get("/static/path-history.js") as resp:
            self.assertEqual(resp.status, 200)
            body = await resp.text()

        self.assertIn("updatePathHistory", body)
        self.assertIn("resetPathHistory", body)

    async def test_events_uses_sse_content_type(self):
        async with self.client.get("/events") as resp:
            try:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers["Content-Type"], "text/event-stream")
            finally:
                resp.close()

    async def test_events_emits_initial_snapshot_when_already_published(self):
        self.store.publish({"hello": "world"})

        async with self.client.get("/events") as resp:
            chunk = await asyncio.wait_for(resp.content.read(4096), timeout=1.0)

        self.assertIn(b'data: {"hello":"world"}\n\n', chunk)

    async def test_events_streams_subsequent_snapshots(self):
        async with self.client.get("/events") as resp:
            self.assertEqual(resp.status, 200)

            target = format_sse_event({"seq": "streamed"})
            publisher = asyncio.create_task(self._publish_repeatedly({"seq": "streamed"}))
            try:
                buffer = await asyncio.wait_for(
                    self._read_until(resp.content, target), timeout=2.0
                )
            finally:
                publisher.cancel()
                try:
                    await publisher
                except asyncio.CancelledError:
                    pass

        self.assertIn(target, buffer)

    async def test_get_config_vision_returns_fields_and_default_values(self):
        async with self.client.get("/config/vision") as resp:
            self.assertEqual(resp.status, 200)
            payload = await resp.json()

        keys = {field["key"] for field in payload["fields"]}
        self.assertEqual(
            keys,
            {
                "enabled",
                "detection_rate_hz",
                "detection_max_width",
                "haar_scale_factor",
                "haar_min_size",
            },
        )
        types = {field["key"]: field["type"] for field in payload["fields"]}
        self.assertEqual(
            types,
            {
                "enabled": "boolean",
                "detection_rate_hz": "number",
                "detection_max_width": "number",
                "haar_scale_factor": "number",
                "haar_min_size": "number",
            },
        )
        self.assertIn("enabled", payload["values"])
        self.assertIn("detection_rate_hz", payload["values"])
        self.assertIn("detection_max_width", payload["values"])
        self.assertIn("haar_scale_factor", payload["values"])
        self.assertIn("haar_min_size", payload["values"])

    async def test_post_config_vision_writes_file_to_disk(self):
        body = {
            "enabled": False,
            "detection_rate_hz": 1.5,
            "detection_max_width": 480,
            "haar_scale_factor": 1.2,
            "haar_min_size": 32,
        }
        async with self.client.post("/config/vision", json=body) as resp:
            self.assertEqual(resp.status, 200)
            payload = await resp.json()

        self.assertTrue(payload["ok"])
        with open(self.vision_config_path) as file_obj:
            saved = json.load(file_obj)
        self.assertEqual(
            saved,
            {
                "enabled": False,
                "detection_rate_hz": 1.5,
                "detection_max_width": 480,
                "haar_scale_factor": 1.2,
                "haar_min_size": 32,
            },
        )

    async def test_post_config_vision_does_not_call_restart_gamepad_teleop(self):
        body = {"enabled": True, "detection_rate_hz": 2.0}
        with mock.patch("robot_web_dashboard.restart_gamepad_teleop") as restart:
            async with self.client.post("/config/vision", json=body) as resp:
                self.assertEqual(resp.status, 200)
        restart.assert_not_called()

    async def test_post_config_vision_rejects_non_object_body(self):
        async with self.client.post("/config/vision", json=[1, 2, 3]) as resp:
            self.assertEqual(resp.status, 400)
            payload = await resp.json()
        self.assertIn("error", payload)

    async def test_get_config_voice_returns_fields_and_default_values(self):
        async with self.client.get("/config/voice") as resp:
            self.assertEqual(resp.status, 200)
            payload = await resp.json()

        keys = {field["key"] for field in payload["fields"]}
        self.assertNotIn("enabled", keys)
        self.assertIn("wake_word_enabled", keys)
        self.assertIn("wake_threshold", keys)
        self.assertIn("wake_chime_path", keys)
        self.assertIn("input_device", keys)
        self.assertIn("output_device", keys)
        self.assertIn("capture_channel_index", keys)
        self.assertIn("input_gain", keys)
        self.assertIn("output_gain", keys)
        self.assertIn("openai_model", keys)
        self.assertNotIn("personality", keys)
        self.assertIn("vad_silence_threshold_secs", keys)
        self.assertIn("barge_in_enabled", keys)
        self.assertIn("barge_in_min_rms", keys)
        self.assertIn("barge_in_sustain_ms", keys)
        self.assertNotIn("barge_in_playback_leakage_ratio", keys)
        types = {field["key"]: field["type"] for field in payload["fields"]}
        self.assertEqual(types["wake_word_enabled"], "boolean")
        self.assertEqual(types["input_device"], "text")
        self.assertEqual(types["capture_channel_index"], "number")
        self.assertEqual(types["input_gain"], "number")
        self.assertEqual(types["output_gain"], "number")
        self.assertEqual(types["openai_model"], "select")
        self.assertIn("enabled", payload["values"])
        self.assertIn("wake_word_enabled", payload["values"])
        self.assertIn("wake_threshold", payload["values"])
        self.assertIn("input_device", payload["values"])
        self.assertIn("openai_model", payload["values"])
        self.assertIn("vad_silence_threshold_secs", payload["values"])
        self.assertIn("personality", payload["values"])
        self.assertIn("barge_in_enabled", payload["values"])
        self.assertIn("barge_in_min_rms", payload["values"])

    async def test_post_config_voice_writes_file_to_disk(self):
        body = {
            "enabled": True,
            "wake_word_enabled": True,
            "wake_threshold": 0.42,
            "input_device": "hw:1,0",
            "output_device": "plughw:1,0",
            "capture_channel_index": 0,
            "input_gain": 1.4,
            "output_gain": 0.8,
            "personality": "scientist",
        }
        async with self.client.post("/config/voice", json=body) as resp:
            self.assertEqual(resp.status, 200)
            payload = await resp.json()

        self.assertTrue(payload["ok"])
        with open(self.voice_config_path) as file_obj:
            saved = json.load(file_obj)
        self.assertTrue(saved["enabled"])
        self.assertTrue(saved["wake_word_enabled"])
        self.assertEqual(saved["wake_threshold"], 0.42)
        self.assertEqual(saved["input_device"], "hw:1,0")
        self.assertEqual(saved["output_device"], "plughw:1,0")
        self.assertEqual(saved["capture_channel_index"], 0)
        self.assertEqual(saved["input_gain"], 1.4)
        self.assertEqual(saved["output_gain"], 0.8)
        self.assertEqual(saved["personality"], "scientist")

    async def test_post_config_voice_partial_merge_preserves_other_keys(self):
        with open(self.voice_config_path, "w") as file_obj:
            json.dump(
                {
                    "enabled": False,
                    "input_device": "hw:2,0",
                    "output_device": "plughw:2,0",
                    "capture_channel_index": 1,
                    "input_gain": 1.0,
                    "output_gain": 1.0,
                },
                file_obj,
            )

        async with self.client.post("/config/voice", json={"input_gain": 1.5}) as resp:
            self.assertEqual(resp.status, 200)
            payload = await resp.json()
        self.assertTrue(payload["ok"])

        with open(self.voice_config_path) as file_obj:
            saved = json.load(file_obj)
        self.assertEqual(saved["input_gain"], 1.5)
        self.assertEqual(saved["input_device"], "hw:2,0")
        self.assertFalse(saved["enabled"])

    async def test_post_config_voice_treats_wake_as_enabled_alias(self):
        async with self.client.post("/config/voice", json={"wake_word_enabled": True}) as resp:
            self.assertEqual(resp.status, 200)

        with open(self.voice_config_path) as file_obj:
            saved = json.load(file_obj)
        self.assertTrue(saved["enabled"])
        self.assertTrue(saved["wake_word_enabled"])

        async with self.client.post("/config/voice", json={"wake_word_enabled": False}) as resp:
            self.assertEqual(resp.status, 200)

        with open(self.voice_config_path) as file_obj:
            saved = json.load(file_obj)
        self.assertFalse(saved["enabled"])
        self.assertFalse(saved["wake_word_enabled"])

    async def test_get_voice_personalities_lists_cards(self):
        async with self.client.get("/voice/personalities") as resp:
            self.assertEqual(resp.status, 200)
            payload = await resp.json()
        self.assertIn("default", payload["personalities"])

    async def test_set_personality_command_requires_name(self):
        async with self.client.post("/voice/command", json={"cmd": "set_personality"}) as resp:
            self.assertEqual(resp.status, 400)

    async def test_voice_command_maps_ignored_ack_to_409(self):
        with mock.patch(
            "robot_web_dashboard.send_voice_command",
            return_value={"ok": True, "accepted": False, "reason": "not_armed"},
        ):
            async with self.client.post("/voice/command", json={"cmd": "talk_now"}) as resp:
                self.assertEqual(resp.status, 409)
                payload = await resp.json()
        self.assertEqual(payload["reason"], "not_armed")

    async def test_get_config_sensors_returns_fields_and_default_values(self):
        async with self.client.get("/config/sensors") as resp:
            self.assertEqual(resp.status, 200)
            payload = await resp.json()

        keys = {field["key"] for field in payload["fields"]}
        self.assertEqual(
            keys,
            {
                "enabled",
                "poll_rate_hz",
                "safety_enabled",
                "cliff_trip_above_mm",
                "forward_stop_below_mm",
            },
        )
        self.assertIn("safety_enabled", payload["values"])
        self.assertIn("cliff_trip_above_mm", payload["values"])

    async def test_post_config_sensors_writes_nested_safety_to_disk(self):
        body = {
            "enabled": True,
            "poll_rate_hz": 8.0,
            "safety_enabled": True,
            "cliff_trip_above_mm": 180,
            "forward_stop_below_mm": 120,
        }
        with (
            mock.patch("robot_web_dashboard.restart_robot_sensors") as restart_sensors,
            mock.patch("robot_web_dashboard.restart_robot_motion") as restart_motion,
        ):
            restart_sensors.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            restart_motion.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            async with self.client.post("/config/sensors", json=body) as resp:
                self.assertEqual(resp.status, 200)
                payload = await resp.json()

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["values"]["safety_enabled"])
        with open(self.sensors_config_path) as file_obj:
            saved = json.load(file_obj)
        self.assertTrue(saved["safety"]["enabled"])
        self.assertEqual(saved["safety"]["cliff_trip_above_mm"], 180)
        self.assertEqual(saved["poll_rate_hz"], 8.0)
        restart_sensors.assert_called_once()
        restart_motion.assert_called_once()

    async def test_post_config_sensors_partial_merge_preserves_sensor_list(self):
        with open(self.sensors_config_path, "w") as file_obj:
            json.dump(
                {
                    "enabled": False,
                    "poll_rate_hz": 5.0,
                    "safety": {"enabled": False, "cliff_trip_above_mm": 200, "forward_stop_below_mm": 150},
                    "sensors": [
                        {"name": "cliff_left", "kind": "vl53l0x", "mux_channel": 0, "role": "cliff"},
                    ],
                },
                file_obj,
            )

        with (
            mock.patch("robot_web_dashboard.restart_robot_sensors") as restart_sensors,
            mock.patch("robot_web_dashboard.restart_robot_motion") as restart_motion,
        ):
            restart_sensors.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            restart_motion.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            async with self.client.post("/config/sensors", json={"safety_enabled": True}) as resp:
                self.assertEqual(resp.status, 200)

        with open(self.sensors_config_path) as file_obj:
            saved = json.load(file_obj)
        self.assertTrue(saved["safety"]["enabled"])
        self.assertEqual(len(saved["sensors"]), 1)

    async def test_config_unknown_name_returns_404(self):
        async with self.client.get("/config/nope") as resp:
            self.assertEqual(resp.status, 404)

    async def _wait_for_redeploy_result(self, expected: str, timeout: float = 2.0):
        deadline = asyncio.get_running_loop().time() + timeout
        status = {}
        while asyncio.get_running_loop().time() < deadline:
            async with self.client.get("/redeploy/status") as resp:
                status = await resp.json()
            if not status["running"] and status.get("last_result") == expected:
                return status
            await asyncio.sleep(0.02)
        self.fail(f"timed out waiting for redeploy {expected!r}; last status={status!r}")

    async def test_redeploy_status_reports_success_with_message(self):
        def fake_stream(command, on_line, *, env=None):
            on_line("Redeploy complete.")
            return 0

        with mock.patch("robot_web_dashboard.stream_command_output", side_effect=fake_stream):
            async with self.client.post("/redeploy/arm") as resp:
                self.assertEqual(resp.status, 200)
            async with self.client.post("/redeploy/run") as resp:
                self.assertEqual(resp.status, 200)

            status = await self._wait_for_redeploy_result("success")
            self.assertEqual(status["last_message"], "Redeploy complete.")
            self.assertGreater(status["result_serial"], 0)

            with open(self.redeploy_status_path) as file_obj:
                saved = json.load(file_obj)
            self.assertEqual(saved["last_result"], "success")

    async def test_redeploy_status_survives_dashboard_restart(self):
        Path(self.redeploy_status_path).write_text(
            json.dumps(
                {
                    "last_result": "success",
                    "last_message": "Redeploy complete.",
                }
            )
            + "\n"
        )

        new_state = WebDashboardState(
            asyncio.get_running_loop(),
            self.store,
            STATIC_DIR,
            self.drive_tuning_config_path,
            self.vision_config_path,
            self.voice_config_path,
            self.voice_command_socket,
            self.sensors_config_path,
            self.redeploy_status_path,
        )

        status = new_state.redeploy_status()
        self.assertEqual(status["last_result"], "success")
        self.assertEqual(status["last_message"], "Redeploy complete.")
        self.assertGreater(status["result_serial"], 0)

    async def test_redeploy_status_reports_failure_with_message(self):
        def fake_stream(command, on_line, *, env=None):
            on_line("Refusing to redeploy: working tree has local changes.")
            return 1

        with mock.patch("robot_web_dashboard.stream_command_output", side_effect=fake_stream):
            async with self.client.post("/redeploy/arm") as resp:
                self.assertEqual(resp.status, 200)
            async with self.client.post("/redeploy/run") as resp:
                self.assertEqual(resp.status, 200)

            status = await self._wait_for_redeploy_result("failed")
            self.assertIn("local changes", status["last_message"])

    async def test_redeploy_keeps_script_success_when_exit_interrupted(self):
        def fake_stream(command, on_line, *, env=None):
            Path(self.redeploy_status_path).write_text(
                json.dumps(
                    {
                        "last_result": "success",
                        "last_message": "Redeploy complete.",
                        "restart_dashboard": True,
                    }
                )
                + "\n"
            )
            on_line("robot-web-dashboard.service will restart when redeploy finishes")
            return -15

        with (
            mock.patch("robot_web_dashboard.stream_command_output", side_effect=fake_stream),
            mock.patch("robot_web_dashboard.restart_web_dashboard") as restart_mock,
        ):
            restart_mock.return_value.returncode = 0
            async with self.client.post("/redeploy/arm") as resp:
                self.assertEqual(resp.status, 200)
            async with self.client.post("/redeploy/run") as resp:
                self.assertEqual(resp.status, 200)

            status = await self._wait_for_redeploy_result("success")
            self.assertEqual(status["last_message"], "Redeploy complete.")
            restart_mock.assert_called_once()

            with open(self.redeploy_status_path) as file_obj:
                saved = json.load(file_obj)
            self.assertEqual(saved["last_result"], "success")

    async def test_redeploy_reports_dashboard_restart_failure(self):
        def fake_stream(command, on_line, *, env=None):
            Path(self.redeploy_status_path).write_text(
                json.dumps(
                    {
                        "last_result": "success",
                        "last_message": "Redeploy complete.",
                        "restart_dashboard": True,
                    }
                )
                + "\n"
            )
            return -15

        with (
            mock.patch("robot_web_dashboard.stream_command_output", side_effect=fake_stream),
            mock.patch("robot_web_dashboard.restart_web_dashboard") as restart_mock,
        ):
            restart_mock.return_value.returncode = 1
            restart_mock.return_value.stderr = "systemctl failed"
            restart_mock.return_value.stdout = ""

            async with self.client.post("/redeploy/arm") as resp:
                self.assertEqual(resp.status, 200)
            async with self.client.post("/redeploy/run") as resp:
                self.assertEqual(resp.status, 200)

            status = await self._wait_for_redeploy_result("failed")
            self.assertIn("dashboard restart failed", status["last_message"])
            self.assertIn("systemctl failed", status["last_message"])

    async def _publish_repeatedly(self, snapshot):
        while True:
            self.store.publish(snapshot)
            await asyncio.sleep(0.01)

    @staticmethod
    async def _read_until(content, marker):
        buffer = b""
        while marker not in buffer:
            chunk = await content.read(4096)
            if not chunk:
                raise AssertionError(
                    f"stream closed before {marker!r}; got {buffer!r}"
                )
            buffer += chunk
        return buffer


class DashboardJsTest(unittest.TestCase):
    """Verifies dashboard ES modules (camera hostname, redeploy, voice, etc.)."""

    @staticmethod
    def _static_dir() -> Path:
        return Path(ROOT) / "src" / "web_dashboard_static"

    def _module(self, name: str) -> str:
        return (self._static_dir() / name).read_text()

    def setUp(self):
        static_dir = self._static_dir()
        self.camera_js = self._module("camera.js")
        self.redeploy_js = self._module("redeploy.js")
        self.telemetry_js = self._module("telemetry.js")
        self.voice_js = self._module("voice.js")
        self.voice_timeline_js = self._module("voice-timeline.js")
        self.voice_turn_stats_js = self._module("voice-turn-stats.js")
        self.config_js = self._module("config.js")
        self.config_store_js = self._module("config-store.js")
        self.dom_js = self._module("dom.js")
        self.logs_js = self._module("logs.js")
        self.main_js = self._module("main.js")
        self.path_history_js = self._module("path-history.js")
        self.dashboard_html = (static_dir / "index.html").read_text()
        self.dashboard_css = (static_dir / "dashboard.css").read_text()

    def test_index_loads_main_module(self):
        self.assertIn('type="module"', self.dashboard_html)
        self.assertIn("/static/main.js", self.dashboard_html)

    def test_path_history_is_initialized_and_fed_snapshots(self):
        self.assertIn("initPathHistory", self.main_js)
        self.assertIn("updatePathHistory(snapshot)", self.telemetry_js)

    def test_path_section_has_canvas_and_controls(self):
        self.assertIn('id="path-canvas"', self.dashboard_html)
        self.assertIn('id="path-reset"', self.dashboard_html)
        self.assertIn('id="path-maximize"', self.dashboard_html)
        self.assertIn("Estimated Local Path", self.dashboard_html)

    def test_path_history_dead_reckons_with_yaw(self):
        self.assertIn("Math.cos(yawRad)", self.path_history_js)
        self.assertIn("Math.sin(yawRad)", self.path_history_js)
        self.assertIn("awaiting odometry", self.path_history_js)
        self.assertIn("awaiting imu", self.path_history_js)
        self.assertIn("stale telemetry", self.path_history_js)

    def test_camera_url_uses_window_location_hostname(self):
        self.assertIn("window.location.hostname", self.camera_js)

    def test_camera_url_does_not_hardcode_loopback_or_localhost(self):
        self.assertNotIn("127.0.0.1", self.camera_js)
        self.assertNotIn("localhost", self.camera_js)

    def test_camera_url_targets_default_camera_port(self):
        self.assertIn(":8081/stream.mjpg", self.camera_js)

    def test_camera_stream_reconnects_after_error(self):
        self.assertIn("camera.addEventListener('error', scheduleCameraReconnect)", self.camera_js)
        self.assertIn("refreshCameraStream()", self.camera_js)

    def test_redeploy_status_is_polled_until_cleared(self):
        self.assertIn("setInterval(refreshRedeployStatus, 1000)", self.redeploy_js)
        self.assertIn("fetch('/redeploy/status')", self.redeploy_js)
        self.assertIn("redeployArmedUntil = Date.now() + 10000", self.redeploy_js)

    def test_redeploy_clicks_queue_work_without_blocking(self):
        self.assertIn("redeployWork = redeployWork.then", self.redeploy_js)
        self.assertNotIn("redeployRequestInFlight", self.redeploy_js)
        self.assertNotIn("if (redeployRequestInFlight) return", self.redeploy_js)
        self.assertIn("button.disabled = false", self.redeploy_js)
        self.assertIn("syncRedeployFromServer", self.redeploy_js)

    def test_redeploy_shows_alert_and_refreshes_on_success(self):
        self.assertIn('id="redeploy-alert"', self.dashboard_html)
        self.assertIn(".redeploy-alert.ok", self.dashboard_css)
        self.assertIn(".redeploy-alert.err", self.dashboard_css)
        self.assertIn("location.reload()", self.redeploy_js)
        self.assertIn("Redeploy succeeded:", self.redeploy_js)
        self.assertIn("Redeploy failed:", self.redeploy_js)
        self.assertIn("result_serial", self.redeploy_js)
        self.assertIn("clearTimeout(redeployReloadTimer)", self.redeploy_js)
        self.assertIn("showRedeployAlert('failed', String(err))", self.redeploy_js)

    def test_fix_wraparound_uses_safe_integer_exponent_not_signed_shift(self):
        self.assertIn("const max = (2 ** 31) - 1;", self.telemetry_js)
        self.assertIn("const min = -(2 ** 31);", self.telemetry_js)
        self.assertNotIn("1 << 31", self.telemetry_js)

    def test_face_overlay_element_lives_inside_camera_section(self):
        self.assertIn('id="face-overlay"', self.dashboard_html)
        section_start = self.dashboard_html.index('id="camera-section"')
        section_end = self.dashboard_html.index("</section>", section_start)
        self.assertIn('id="face-overlay"', self.dashboard_html[section_start:section_end])

    def test_face_overlay_does_not_intercept_pointer_events(self):
        self.assertIn("#face-overlay", self.dashboard_css)
        self.assertIn("pointer-events: none", self.dashboard_css)

    def test_overlay_clears_when_vision_source_is_stale(self):
        self.assertIn("visionSource.stale === true", self.camera_js)

    def test_overlay_clears_when_last_detection_is_old(self):
        self.assertIn("VISION_STALE_SECONDS", self.camera_js)
        self.assertIn("snapshotTime - lastDetection", self.camera_js)

    def test_overlay_handles_letterboxing_via_contained_rect(self):
        self.assertIn("containedImageRect", self.camera_js)
        self.assertIn("sourceAspect", self.camera_js)

    def test_overlay_uses_camera_section_size_without_affecting_layout(self):
        self.assertIn("cameraSection.clientWidth", self.camera_js)
        self.assertIn("cameraSection.clientHeight", self.camera_js)
        self.assertIn("position: absolute", self.dashboard_css)

    def test_dashboard_renders_voice_status_panel(self):
        self.assertIn('id="voice-rows"', self.dashboard_html)
        self.assertIn("export function renderVoice(snapshot, sources)", self.voice_js)
        self.assertIn("renderVoice(snapshot, sources)", self.telemetry_js)

    def test_dashboard_logs_include_all_robot_services(self):
        command = " ".join(LOG_COMMAND)
        for service in (
            "robot-brain",
            "robot-telemetry",
            "robot-battery",
            "robot-pi-battery",
            "robot-motion",
            "gamepad-teleop",
            "robot-camera",
            "robot-vision",
            "robot-sensors",
            "robot-voice",
            "robot-web-dashboard",
        ):
            self.assertIn(service, command)

    def test_dashboard_renders_motor_rail_telemetry(self):
        self.assertIn("const motorRail = snapshot.motor_rail || {}", self.telemetry_js)
        self.assertIn("renderBattery(battery, motorRail, sources.motor_rail || {}, piBattery, sources.pi_battery || {})", self.telemetry_js)
        self.assertIn("row('rail'", self.telemetry_js)
        self.assertIn("row('motor est'", self.telemetry_js)
        self.assertIn("battery.stale ? 'warn'", self.telemetry_js)
        self.assertIn("' STALE'", self.telemetry_js)
        self.assertIn("low_battery_cutoff", self.telemetry_js)

    def test_dashboard_renders_pi_battery_telemetry(self):
        self.assertIn("const piBattery = snapshot.pi_battery || {}", self.telemetry_js)
        self.assertIn("row('pi ups'", self.telemetry_js)
        self.assertIn("row('pi runtime'", self.telemetry_js)
        self.assertIn("row('pi status'", self.telemetry_js)
        self.assertIn("history.pi_pack_voltage", self.telemetry_js)

    def test_dashboard_exposes_barge_in_visibility_not_inline_editors(self):
        self.assertNotIn("voiceToggleRow('barge-in'", self.voice_js)
        self.assertNotIn("barge_in_min_rms", self.voice_js)
        self.assertNotIn("barge_in_event", self.voice_js)
        self.assertIn("barge_in_fired", self.voice_timeline_js)
        self.assertIn("barge_in_considered", self.voice_timeline_js)
        self.assertIn("echo_suppressed", self.voice_timeline_js)

    def test_voice_turn_stats_show_token_and_audio_timing(self):
        self.assertIn("case 'turn_first_token':", self.voice_turn_stats_js)
        self.assertIn("case 'assistant_start':", self.voice_turn_stats_js)
        self.assertIn("first_token_ms", self.voice_turn_stats_js)
        self.assertIn("first_audio_ms", self.voice_turn_stats_js)
        self.assertIn("token ${row.first_token_ms}ms", self.voice_turn_stats_js)
        self.assertIn("audio ${row.first_audio_ms}ms", self.voice_turn_stats_js)

    def test_header_has_voice_toggle_button(self):
        self.assertIn('id="voice-toggle-button"', self.dashboard_html)
        self.assertIn("onVoiceToggle", self.voice_js)
        self.assertIn("updateVoiceToggleButton", self.voice_js)
        self.assertIn(".record-dot", self.dashboard_css)

    def test_maximized_voice_timeline_has_voice_toggle(self):
        self.assertIn('id="voice-timeline-toggle-button"', self.dashboard_html)
        self.assertIn("voice-timeline-toggle", self.dashboard_html)
        self.assertIn("#voice-timeline-section.maximized .voice-timeline-toggle", self.dashboard_css)
        self.assertIn("bindOn('voice-timeline-toggle-button', 'click', onVoiceToggle)", self.voice_js)

    def test_voice_toggle_button_has_only_four_labels(self):
        self.assertIn("'Voice Off'", self.voice_js)
        self.assertIn("'Voice On'", self.voice_js)
        self.assertIn("'Starting'", self.voice_js)
        self.assertIn("'Stopping'", self.voice_js)
        self.assertNotIn("'Speaking'", self.voice_js)
        self.assertNotIn("'Listening'", self.voice_js)
        self.assertNotIn("'Voice Error'", self.voice_js)

    def test_voice_toggle_uses_want_state_and_config_store(self):
        self.assertIn("voiceWantEnabled", self.voice_js)
        self.assertIn("voiceTelemetryEnabled", self.voice_js)
        self.assertIn("voiceWantEnabled = !voiceWantEnabled", self.voice_js)
        self.assertIn("voiceUiPending", self.voice_js)
        self.assertIn("{ enabled: true, wake_word_enabled: true }", self.voice_js)
        self.assertIn("{ enabled: false, wake_word_enabled: false }", self.voice_js)
        self.assertIn("canControlSession", self.voice_js)
        self.assertNotIn("pending || stale", self.voice_js)
        self.assertIn("configStore.voice.set(patch)", self.voice_js)
        self.assertIn("configStore.voice.flush()", self.voice_js)
        self.assertNotIn("voicePersistWork", self.voice_js)
        self.assertNotIn("fetchVoiceValues", self.voice_js)

    def test_talk_now_button_is_disabled_when_wake_is_off(self):
        self.assertIn("const wakeOn = displayWakeEnabled()", self.voice_js)
        self.assertIn("const canUse = wakeOn && canControlSession(voice)", self.voice_js)
        self.assertIn("if (!latestVoice || !displayWakeEnabled() || !canControlSession(latestVoice)) return", self.voice_js)

    def test_button_binding_tolerates_missing_elements(self):
        self.assertIn("export function on(id, eventName, handler)", self.dom_js)
        self.assertIn("if (element) {", self.dom_js)
        self.assertIn("element.addEventListener", self.dom_js)

    def test_action_buttons_use_stable_click_handlers(self):
        self.assertIn("bindOn('voice-toggle-button', 'click', onVoiceToggle)", self.voice_js)
        self.assertIn("bindOn('redeploy-button', 'click', onRedeploy)", self.redeploy_js)
        self.assertNotIn("runActionButton", self.voice_js)
        self.assertNotIn("setInterval(updateRedeployButton", self.redeploy_js)
        self.assertIn("bindVoiceHandlers", self.main_js)
        self.assertIn("bindRedeployHandlers", self.main_js)

    def test_action_buttons_are_not_rewritten_when_state_is_unchanged(self):
        self.assertIn("querySelectorAll('.voice-toggle')", self.voice_js)
        self.assertIn("if (button.className !== nextClassName) button.className = nextClassName", self.voice_js)
        self.assertIn("if (label && label.textContent !== text) label.textContent = text", self.voice_js)
        self.assertIn("lastTalkButtonState", self.voice_js)
        self.assertIn("if (stateLabel === lastTalkButtonState) return", self.voice_js)
        self.assertIn("pendingPersonality", self.voice_js)
        self.assertIn("if (button.textContent !== text) button.textContent = text", self.redeploy_js)
        self.assertIn("scheduleRedeployDisarm", self.redeploy_js)

    def test_voice_card_shows_activity_status_prominently(self):
        self.assertIn("voiceCardStatus", self.voice_js)
        self.assertIn("voiceActivityRow", self.voice_js)
        self.assertIn("voice-activity-row", self.voice_js)
        self.assertIn(".voice-activity-row .voice-activity", self.dashboard_css)
        self.assertIn("voiceStatusClass(status, lastError)", self.voice_js)

    def test_voice_card_has_gain_controls(self):
        self.assertIn("gainControlRow('mic gain', 'input_gain'", self.voice_js)
        self.assertIn("gainControlRow('speaker', 'output_gain'", self.voice_js)
        self.assertIn("onVoiceGainCommit", self.voice_js)
        self.assertIn("configStore.voice.set", self.voice_js)
        self.assertIn("configStore.voice.flush()", self.voice_js)
        self.assertIn("configStore.voice.get", self.voice_js)
        self.assertIn("data-voice-key", self.voice_js)
        self.assertNotIn("queueGainSave", self.voice_js)
        self.assertNotIn("gainSaveTimers", self.voice_js)

    def test_config_store_owns_save_queue(self):
        self.assertIn("saveWork", self.config_store_js)
        self.assertIn("set(partial)", self.config_store_js)
        self.assertIn("flush()", self.config_store_js)
        self.assertIn("debounceTimer", self.config_store_js)
        self.assertIn("runSave", self.config_store_js)
        self.assertIn("export const configStore", self.config_store_js)
        self.assertNotIn("patch(partial)", self.config_store_js)

    def test_config_store_pending_local_and_ingest(self):
        self.assertIn("Object.assign(section.local, partial)", self.config_store_js)
        self.assertIn("valuesEqual(incoming, section.local[key])", self.config_store_js)
        self.assertIn("const submitted = {}", self.config_store_js)
        self.assertIn("dirtyKeys", self.config_store_js)

    def test_config_renderer_supports_text_fields_without_number_coercion(self):
        self.assertIn("field.type === 'text'", self.config_js)
        self.assertIn("target[input.dataset.key] = input.value", self.config_js)
        self.assertIn("configStore.voice.apply", self.config_js)
        self.assertIn("loadAll", self.config_js)
        self.assertNotIn("fetchVoiceValues", self.voice_js)

    def test_config_renderer_supports_select_fields(self):
        self.assertIn("field.type === 'select'", self.config_js)
        self.assertIn("<select", self.config_js)
        self.assertIn("select[data-section]", self.config_js)
        self.assertIn("input.tagName === 'SELECT'", self.config_js)

    def test_config_modal_fields_scroll_inside_viewport(self):
        self.assertIn("max-height: calc(100vh - 2rem)", self.dashboard_css)
        self.assertIn("grid-template-rows: auto auto minmax(0, 1fr) auto auto", self.dashboard_css)
        self.assertIn("overflow-y: auto", self.dashboard_css)

    def test_logs_follow_bottom_only_when_at_bottom(self):
        self.assertIn("let followBottom = true", self.logs_js)
        self.assertIn("function logsAtBottom(output)", self.logs_js)
        self.assertIn("if (followBottom) {", self.logs_js)
        self.assertIn("output.scrollTop = output.scrollHeight", self.logs_js)

    def test_logs_scroll_listener_updates_follow_bottom(self):
        self.assertIn("export function bindLogScroll(bindOn)", self.logs_js)
        self.assertIn("bindOn('logs-output', 'scroll'", self.logs_js)
        self.assertIn("followBottom = logsAtBottom(output)", self.logs_js)
        self.assertIn("bindLogScroll(on)", self.main_js)


if __name__ == "__main__":
    unittest.main()
