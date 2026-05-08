import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

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
        self.store = SnapshotStore(asyncio.get_running_loop())
        self.state = WebDashboardState(self.store, STATIC_DIR)
        self.client = TestClient(TestServer(build_app(self.state)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_index_serves_dashboard_html(self):
        async with self.client.get("/") as resp:
            self.assertEqual(resp.status, 200)
            body = await resp.text()

        self.assertIn("Robo-Pet Dashboard", body)
        self.assertIn("/static/dashboard.css", body)
        self.assertIn("/static/dashboard.js", body)

    async def test_static_dashboard_js_is_served(self):
        async with self.client.get("/static/dashboard.js") as resp:
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
    """Verifies the camera URL is built from the page hostname, not loopback."""

    def setUp(self):
        self.dashboard_js = (
            Path(ROOT) / "src" / "web_dashboard_static" / "dashboard.js"
        ).read_text()

    def test_camera_url_uses_window_location_hostname(self):
        self.assertIn("window.location.hostname", self.dashboard_js)

    def test_camera_url_does_not_hardcode_loopback_or_localhost(self):
        self.assertNotIn("127.0.0.1", self.dashboard_js)
        self.assertNotIn("localhost", self.dashboard_js)

    def test_camera_url_targets_default_camera_port(self):
        self.assertIn(":8081/stream.mjpg", self.dashboard_js)

    def test_fix_wraparound_uses_safe_integer_exponent_not_signed_shift(self):
        self.assertIn("const max = (2 ** 31) - 1;", self.dashboard_js)
        self.assertIn("const min = -(2 ** 31);", self.dashboard_js)
        self.assertNotIn("1 << 31", self.dashboard_js)


if __name__ == "__main__":
    unittest.main()
