import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))


ROBOT_CAMERA_IMPORT_ERROR = None
try:
    from aiohttp.test_utils import TestClient, TestServer

    from robot_camera import (
        CAPTURE_FAILURE_HEALTH_THRESHOLD,
        CameraServiceState,
        FrameStore,
        MJPEG_BOUNDARY,
        SHUTDOWN_TIMEOUT_SECONDS,
        build_app,
        mjpeg_part,
        wait_for_frame,
    )
except ModuleNotFoundError as exc:
    if exc.name != "aiohttp":
        raise
    ROBOT_CAMERA_IMPORT_ERROR = exc


@unittest.skipIf(ROBOT_CAMERA_IMPORT_ERROR is not None, "aiohttp is not installed")
class FrameStoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_latest_is_none_before_any_publish(self):
        store = FrameStore(asyncio.get_running_loop())

        self.assertIsNone(store.latest())

    async def test_latest_returns_most_recent_published_jpeg(self):
        store = FrameStore(asyncio.get_running_loop())

        store.publish(b"first-jpeg")
        self.assertEqual(store.latest(), b"first-jpeg")

        store.publish(b"second-jpeg")
        self.assertEqual(store.latest(), b"second-jpeg")

    async def test_wait_for_next_returns_frame_published_after_wait(self):
        store = FrameStore(asyncio.get_running_loop())
        store.publish(b"already-here")

        async def publish_after_delay():
            await asyncio.sleep(0.01)
            store.publish(b"streamed")

        publisher = asyncio.create_task(publish_after_delay())
        try:
            result = await asyncio.wait_for(store.wait_for_next(), timeout=1.0)
        finally:
            await publisher

        self.assertEqual(result, b"streamed")


@unittest.skipIf(ROBOT_CAMERA_IMPORT_ERROR is not None, "aiohttp is not installed")
class MjpegPartTest(unittest.TestCase):
    def test_mjpeg_part_emits_boundary_headers_and_trailing_crlf(self):
        chunk = mjpeg_part(b"\xff\xd8\xff\xe0FAKE", boundary="my-boundary")

        self.assertEqual(
            chunk,
            b"--my-boundary\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: 8\r\n"
            b"\r\n"
            b"\xff\xd8\xff\xe0FAKE"
            b"\r\n",
        )

    def test_mjpeg_part_uses_default_boundary_when_unspecified(self):
        chunk = mjpeg_part(b"x")

        self.assertTrue(chunk.startswith(f"--{MJPEG_BOUNDARY}\r\n".encode("ascii")))


@unittest.skipIf(ROBOT_CAMERA_IMPORT_ERROR is not None, "aiohttp is not installed")
class CameraServiceHandlersTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = FrameStore(asyncio.get_running_loop())
        self.state = CameraServiceState(self.store, first_frame_timeout=0.01)
        self.client = TestClient(TestServer(build_app(self.state)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_health_returns_503_when_camera_failed_to_open(self):
        self.state.camera_ok = False
        self.state.error = "fake camera failure"

        async with self.client.get("/health") as resp:
            self.assertEqual(resp.status, 503)
            payload = await resp.json()

        self.assertEqual(
            payload, {"status": "unavailable", "error": "fake camera failure"}
        )

    async def test_health_returns_ok_with_frame_flag_when_camera_running(self):
        self.state.camera_ok = True
        self.store.publish(b"any-jpeg")

        async with self.client.get("/health") as resp:
            self.assertEqual(resp.status, 200)
            payload = await resp.json()

        self.assertEqual(payload, {"status": "ok", "has_frame": True, "active_streams": 0})

    async def test_health_returns_503_after_repeated_capture_failures(self):
        self.state.camera_ok = True
        self.store.publish(b"stale-jpeg")

        for _ in range(CAPTURE_FAILURE_HEALTH_THRESHOLD):
            self.state.record_capture_failure("fake capture failure")

        async with self.client.get("/health") as resp:
            self.assertEqual(resp.status, 503)
            payload = await resp.json()

        self.assertEqual(
            payload,
            {"status": "unavailable", "error": "camera capture failed: fake capture failure"},
        )
        self.assertIsNone(self.store.latest())

    async def test_capture_success_restores_health_after_failures(self):
        self.state.camera_ok = True
        for _ in range(CAPTURE_FAILURE_HEALTH_THRESHOLD):
            self.state.record_capture_failure("fake capture failure")

        self.state.record_capture_success()

        async with self.client.get("/health") as resp:
            self.assertEqual(resp.status, 200)
            payload = await resp.json()

        self.assertEqual(payload, {"status": "ok", "has_frame": False, "active_streams": 0})

    async def test_snapshot_returns_503_before_first_frame(self):
        self.state.camera_ok = True

        async with self.client.get("/snapshot.jpg") as resp:
            self.assertEqual(resp.status, 503)

    async def test_snapshot_returns_latest_jpeg_bytes(self):
        self.state.camera_ok = True
        self.store.publish(b"\xff\xd8\xff\xe0FAKEJPEG")

        async with self.client.get("/snapshot.jpg") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["Content-Type"], "image/jpeg")
            body = await resp.read()

        self.assertEqual(body, b"\xff\xd8\xff\xe0FAKEJPEG")

    async def test_stream_returns_503_before_first_frame(self):
        self.state.camera_ok = True

        async with self.client.get("/stream.mjpg") as resp:
            self.assertEqual(resp.status, 503)

    async def test_stream_emits_published_frame_as_mjpeg_part(self):
        self.state.camera_ok = True
        self.store.publish(b"initial-jpeg")

        async with self.client.get("/stream.mjpg") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(
                resp.headers["Content-Type"],
                f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
            )

            target = mjpeg_part(b"streamed-jpeg")
            publisher = asyncio.create_task(
                self._publish_repeatedly(b"streamed-jpeg")
            )
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

    def test_runner_uses_short_shutdown_timeout_for_open_streams(self):
        with open(os.path.join(ROOT, "src", "robot_camera.py")) as file_obj:
            source = file_obj.read()

        self.assertIn("web.AppRunner(app, shutdown_timeout=SHUTDOWN_TIMEOUT_SECONDS)", source)
        self.assertLessEqual(SHUTDOWN_TIMEOUT_SECONDS, 5.0)

    async def _publish_repeatedly(self, jpeg: bytes) -> None:
        while True:
            self.store.publish(jpeg)
            await asyncio.sleep(0.01)

    @staticmethod
    async def _read_until(content, marker: bytes) -> bytes:
        buffer = b""
        while marker not in buffer:
            chunk = await content.read(4096)
            if not chunk:
                raise AssertionError(
                    f"stream closed before {marker!r}; got {buffer!r}"
                )
            buffer += chunk
        return buffer


@unittest.skipIf(ROBOT_CAMERA_IMPORT_ERROR is not None, "aiohttp is not installed")
class CameraServiceLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_camera_starts_on_demand_and_stops_after_idle_timeout(self):
        store = FrameStore(asyncio.get_running_loop())
        driver = FakeCameraDriver()
        state = CameraServiceState(
            store,
            driver_factory=lambda: driver,
            fps=100.0,
            idle_timeout=0.01,
            first_frame_timeout=0.5,
            loop=asyncio.get_running_loop(),
        )

        self.assertFalse(state.camera_ok)

        self.assertTrue(await state.ensure_started())
        self.assertIsNotNone(await wait_for_frame(state))
        state.schedule_idle_stop()
        await asyncio.sleep(0.05)

        self.assertFalse(driver.started)
        self.assertFalse(state.camera_ok)
        self.assertIsNone(store.latest())

    async def test_active_stream_keeps_camera_running_until_release(self):
        store = FrameStore(asyncio.get_running_loop())
        driver = FakeCameraDriver()
        state = CameraServiceState(
            store,
            driver_factory=lambda: driver,
            fps=100.0,
            idle_timeout=0.01,
            first_frame_timeout=0.5,
            loop=asyncio.get_running_loop(),
        )

        state.acquire_stream()
        self.assertTrue(await state.ensure_started())
        state.schedule_idle_stop()
        await asyncio.sleep(0.05)
        self.assertTrue(driver.started)

        state.release_stream()
        await asyncio.sleep(0.05)
        self.assertFalse(driver.started)

    async def test_concurrent_first_clients_start_camera_once(self):
        store = FrameStore(asyncio.get_running_loop())
        driver = FakeCameraDriver()
        state = CameraServiceState(
            store,
            driver_factory=lambda: driver,
            fps=100.0,
            idle_timeout=0.01,
            first_frame_timeout=0.5,
            loop=asyncio.get_running_loop(),
        )

        await asyncio.gather(state.ensure_started(), state.ensure_started())

        self.assertEqual(driver.starts, 1)
        state.stop_camera(force=True)


class FakeCameraDriver:
    def __init__(self):
        self.started = False
        self.starts = 0
        self.captures = 0

    def start(self):
        self.starts += 1
        self.started = True

    def capture_jpeg(self):
        self.captures += 1
        return f"frame-{self.captures}".encode("ascii")

    def stop(self):
        self.started = False


if __name__ == "__main__":
    unittest.main()
