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
        self.teleop_config_path = os.path.join(self.tmpdir.name, "teleop.json")
        self.vision_config_path = os.path.join(self.tmpdir.name, "vision.json")
        self.voice_config_path = os.path.join(self.tmpdir.name, "voice.json")
        self.store = SnapshotStore(asyncio.get_running_loop())
        self.state = WebDashboardState(
            asyncio.get_running_loop(),
            self.store,
            STATIC_DIR,
            self.teleop_config_path,
            self.vision_config_path,
            self.voice_config_path,
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
        self.assertEqual(keys, {"enabled", "detection_rate_hz"})
        types = {field["key"]: field["type"] for field in payload["fields"]}
        self.assertEqual(types, {"enabled": "boolean", "detection_rate_hz": "number"})
        self.assertIn("enabled", payload["values"])
        self.assertIn("detection_rate_hz", payload["values"])

    async def test_post_config_vision_writes_file_to_disk(self):
        body = {"enabled": False, "detection_rate_hz": 1.5}
        async with self.client.post("/config/vision", json=body) as resp:
            self.assertEqual(resp.status, 200)
            payload = await resp.json()

        self.assertTrue(payload["ok"])
        with open(self.vision_config_path) as file_obj:
            saved = json.load(file_obj)
        self.assertEqual(saved, {"enabled": False, "detection_rate_hz": 1.5})

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
        self.assertIn("enabled", keys)
        self.assertIn("input_device", keys)
        self.assertIn("output_device", keys)
        self.assertIn("capture_channel_index", keys)
        self.assertIn("input_gain", keys)
        self.assertIn("output_gain", keys)
        self.assertIn("barge_in_enabled", keys)
        self.assertIn("barge_in_min_rms", keys)
        self.assertIn("barge_in_sustain_ms", keys)
        self.assertIn("barge_in_playback_leakage_ratio", keys)
        types = {field["key"]: field["type"] for field in payload["fields"]}
        self.assertEqual(types["enabled"], "boolean")
        self.assertEqual(types["input_device"], "text")
        self.assertEqual(types["capture_channel_index"], "number")
        self.assertEqual(types["input_gain"], "number")
        self.assertEqual(types["output_gain"], "number")
        self.assertFalse(payload["values"]["enabled"])
        self.assertEqual(payload["values"]["input_device"], "hw:0,0")
        self.assertTrue(payload["values"]["barge_in_enabled"])
        self.assertEqual(payload["values"]["barge_in_min_rms"], 700)

    async def test_post_config_voice_writes_file_to_disk(self):
        body = {
            "enabled": True,
            "input_device": "hw:1,0",
            "output_device": "plughw:1,0",
            "capture_channel_index": 0,
            "input_gain": 1.4,
            "output_gain": 0.8,
            "voice_id": "voice-a",
            "alternate_voice_id": "voice-b",
        }
        async with self.client.post("/config/voice", json=body) as resp:
            self.assertEqual(resp.status, 200)
            payload = await resp.json()

        self.assertTrue(payload["ok"])
        with open(self.voice_config_path) as file_obj:
            saved = json.load(file_obj)
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["input_device"], "hw:1,0")
        self.assertEqual(saved["output_device"], "plughw:1,0")
        self.assertEqual(saved["capture_channel_index"], 0)
        self.assertEqual(saved["input_gain"], 1.4)
        self.assertEqual(saved["output_gain"], 0.8)
        self.assertEqual(saved["voice_id"], "voice-a")
        self.assertEqual(saved["alternate_voice_id"], "voice-b")

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
        self.config_js = self._module("config.js")
        self.dom_js = self._module("dom.js")
        self.main_js = self._module("main.js")
        self.dashboard_html = (static_dir / "index.html").read_text()
        self.dashboard_css = (static_dir / "dashboard.css").read_text()

    def test_index_loads_main_module(self):
        self.assertIn('type="module"', self.dashboard_html)
        self.assertIn("/static/main.js", self.dashboard_html)

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

    def test_dashboard_exposes_barge_in_tuning_controls(self):
        self.assertIn("barge_in_enabled", self.voice_js)
        self.assertIn("barge_in_min_rms", self.voice_js)
        self.assertIn("barge_in_sustain_ms", self.voice_js)
        self.assertIn("barge_in_playback_leakage_ratio", self.voice_js)
        self.assertIn("barge_in_event", self.voice_js)
        self.assertIn("JUST NOW", self.voice_js)
        self.assertIn("HEARING STT", self.voice_js)
        self.assertIn("barge_in_gate", self.voice_js)
        self.assertIn("barge_in_last_reason", self.voice_js)

    def test_header_has_voice_toggle_button(self):
        self.assertIn('id="voice-toggle-button"', self.dashboard_html)
        self.assertIn("onVoiceToggle", self.voice_js)
        self.assertIn("updateVoiceToggleButton", self.voice_js)
        self.assertIn(".record-dot", self.dashboard_css)

    def test_voice_toggle_button_has_only_four_labels(self):
        self.assertIn("'Voice Off'", self.voice_js)
        self.assertIn("'Voice On'", self.voice_js)
        self.assertIn("'Starting'", self.voice_js)
        self.assertIn("'Stopping'", self.voice_js)
        self.assertNotIn("'Speaking'", self.voice_js)
        self.assertNotIn("'Listening'", self.voice_js)
        self.assertNotIn("'Voice Error'", self.voice_js)

    def test_voice_toggle_uses_want_state_not_merged_toggle(self):
        self.assertIn("voiceWantEnabled", self.voice_js)
        self.assertIn("voiceTelemetryEnabled", self.voice_js)
        self.assertIn("voiceWantEnabled = !voiceWantEnabled", self.voice_js)
        self.assertIn("voiceUiPending", self.voice_js)
        self.assertIn("voicePersistWork = voicePersistWork.then", self.voice_js)
        self.assertNotIn("voiceTargetEnabled", self.voice_js)
        self.assertNotIn("voiceEffectiveEnabled", self.voice_js)
        self.assertNotIn("if (voiceTogglePending) return", self.voice_js)

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
        self.assertIn("if (button.className !== className) button.className = className", self.voice_js)
        self.assertIn("if (label.textContent !== text) label.textContent = text", self.voice_js)
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
        self.assertIn("voicePendingPatch", self.voice_js)
        self.assertIn("data-voice-key", self.voice_js)

    def test_config_renderer_supports_text_fields_without_number_coercion(self):
        self.assertIn("field.type === 'text'", self.config_js)
        self.assertIn("target[input.dataset.key] = input.value", self.config_js)
        self.assertIn("fetch('/config/voice')", self.config_js)

    def test_config_modal_fields_scroll_inside_viewport(self):
        self.assertIn("max-height: calc(100vh - 2rem)", self.dashboard_css)
        self.assertIn("grid-template-rows: auto auto minmax(0, 1fr) auto auto", self.dashboard_css)
        self.assertIn("overflow-y: auto", self.dashboard_css)


if __name__ == "__main__":
    unittest.main()
