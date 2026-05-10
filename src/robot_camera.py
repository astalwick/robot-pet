#!/usr/bin/env python3
"""Camera service: owns the Pi camera and serves snapshots/MJPEG over HTTP.

Single owner of `drivers.camera.CameraDriver`. Other processes (web dashboard,
future perception) consume frames over HTTP rather than opening the camera.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import threading
from collections.abc import Callable

from aiohttp import web

from drivers.camera import CameraDriver, CameraUnavailable
from lib.log import setup_logging
from telemetry.paths import DEFAULT_CAMERA_BIND_HOST, DEFAULT_CAMERA_PORT


DEFAULT_CAMERA_WIDTH = 1280
DEFAULT_CAMERA_HEIGHT = 720
DEFAULT_SENSOR_WIDTH = 2304
DEFAULT_SENSOR_HEIGHT = 1296
DEFAULT_CAPTURE_FPS = 10.0
DEFAULT_JPEG_QUALITY = 75
DEFAULT_IDLE_TIMEOUT_SECONDS = 30.0
FIRST_FRAME_TIMEOUT_SECONDS = 3.0
CAPTURE_FAILURE_HEALTH_THRESHOLD = 3
MJPEG_BOUNDARY = "robotpet-frame"


log = setup_logging("robot-camera")


class FrameStore:
    """Thread-safe latest-JPEG store with async new-frame notifications.

    The capture thread calls `publish` on every captured frame. HTTP handlers
    call `latest` for snapshots and await `wait_for_next` for streaming.
    Slow consumers cannot block the capture thread: publish only updates the
    latest reference and schedules waiter notifications via the loop.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._lock = threading.Lock()
        self._latest: bytes | None = None
        self._waiters: list[asyncio.Event] = []

    def publish(self, jpeg: bytes) -> None:
        with self._lock:
            self._latest = jpeg
            waiters = self._waiters
            self._waiters = []
        for waiter in waiters:
            self._loop.call_soon_threadsafe(waiter.set)

    def latest(self) -> bytes | None:
        with self._lock:
            return self._latest

    def clear(self) -> None:
        with self._lock:
            self._latest = None

    async def wait_for_next(self) -> bytes:
        """Block until the next publish, then return the latest frame."""
        event = asyncio.Event()
        with self._lock:
            self._waiters.append(event)
        try:
            await event.wait()
        except asyncio.CancelledError:
            with self._lock:
                if event in self._waiters:
                    self._waiters.remove(event)
            raise
        latest = self.latest()
        # publish() always sets _latest before notifying waiters.
        assert latest is not None
        return latest


def mjpeg_part(jpeg: bytes, boundary: str = MJPEG_BOUNDARY) -> bytes:
    """Format a single MJPEG multipart chunk."""
    headers = (
        f"--{boundary}\r\n"
        f"Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(jpeg)}\r\n"
        f"\r\n"
    ).encode("ascii")
    return headers + jpeg + b"\r\n"


class CameraCaptureThread:
    """Continuously captures JPEG frames into a FrameStore."""

    def __init__(self, driver: CameraDriver, store: FrameStore, fps: float, state: "CameraServiceState"):
        self._driver = driver
        self._store = store
        self._state = state
        self._period = 1.0 / fps if fps > 0 else 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="camera-capture", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                jpeg = self._driver.capture_jpeg()
            except Exception as exc:  # noqa: BLE001 -- libcamera throws varied types
                log.warning("camera capture failed: %s", exc)
                self._state.record_capture_failure(str(exc))
                self._stop.wait(0.5)
                continue
            self._state.record_capture_success()
            self._store.publish(jpeg)
            if self._period > 0:
                self._stop.wait(self._period)


class CameraServiceState:
    """Owns on-demand camera runtime state for aiohttp handlers."""

    def __init__(
        self,
        store: FrameStore,
        *,
        driver_factory: Callable[[], CameraDriver] | None = None,
        fps: float = DEFAULT_CAPTURE_FPS,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
        first_frame_timeout: float = FIRST_FRAME_TIMEOUT_SECONDS,
        loop: asyncio.AbstractEventLoop | None = None,
    ):
        self.store = store
        self.camera_ok: bool = False
        self.error: str | None = None
        self.consecutive_capture_failures = 0
        self.active_streams = 0

        self._driver_factory = driver_factory
        self._fps = fps
        self._idle_timeout = idle_timeout
        self.first_frame_timeout = first_frame_timeout
        self._loop = loop
        self._driver: CameraDriver | None = None
        self._capture: CameraCaptureThread | None = None
        self._idle_handle: asyncio.TimerHandle | None = None
        self._lock = threading.Lock()
        self._start_lock = asyncio.Lock()

    async def ensure_started(self) -> bool:
        """Start the camera if this service owns a driver and it is idle."""
        async with self._start_lock:
            self._cancel_idle_stop()
            with self._lock:
                if self._capture is not None:
                    return True
                if self._driver_factory is None:
                    return self.camera_ok or self.store.latest() is not None

            driver = self._driver_factory()
            try:
                driver.start()
            except CameraUnavailable as exc:
                log.error("camera unavailable: %s", exc)
                with self._lock:
                    self.camera_ok = False
                    self.error = str(exc)
                return False

            capture = CameraCaptureThread(driver, self.store, self._fps, self)
            self.store.clear()
            capture.start()
            with self._lock:
                self._driver = driver
                self._capture = capture
                self.camera_ok = True
                self.error = None
                self.consecutive_capture_failures = 0
            log.info("camera became active")
            return True

    def record_capture_success(self) -> None:
        with self._lock:
            self.consecutive_capture_failures = 0
            self.camera_ok = True
            self.error = None

    def record_capture_failure(self, reason: str) -> None:
        with self._lock:
            self.consecutive_capture_failures += 1
            if self.consecutive_capture_failures < CAPTURE_FAILURE_HEALTH_THRESHOLD:
                return
            self.camera_ok = False
            self.error = f"camera capture failed: {reason}"
        self.store.clear()

    def acquire_stream(self) -> None:
        self._cancel_idle_stop()
        with self._lock:
            self.active_streams += 1

    def release_stream(self) -> None:
        with self._lock:
            self.active_streams -= 1
            active_streams = self.active_streams
        if active_streams == 0:
            self.schedule_idle_stop()

    def schedule_idle_stop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        self._cancel_idle_stop()
        if self._idle_timeout <= 0:
            self.stop_camera()
            return
        self._idle_handle = loop.call_later(self._idle_timeout, self.stop_camera)

    def stop_camera(self, *, force: bool = False) -> None:
        with self._lock:
            if self.active_streams > 0 and not force:
                return
            capture = self._capture
            driver = self._driver
            self._capture = None
            self._driver = None
            self.camera_ok = False
            self.consecutive_capture_failures = 0
            self._idle_handle = None
        if capture is not None:
            capture.stop()
        if driver is not None:
            driver.stop()
        self.store.clear()
        if capture is not None or driver is not None:
            log.info("camera became idle")

    def _cancel_idle_stop(self) -> None:
        handle = self._idle_handle
        if handle is not None:
            handle.cancel()
            self._idle_handle = None


async def health_handler(request: web.Request) -> web.Response:
    state: CameraServiceState = request.app["state"]
    if state.error is not None:
        return web.json_response(
            {"status": "unavailable", "error": state.error},
            status=503,
        )
    return web.json_response(
        {
            "status": "ok" if state.camera_ok else "idle",
            "has_frame": state.store.latest() is not None,
            "active_streams": state.active_streams,
        }
    )


async def wait_for_frame(state: CameraServiceState) -> bytes | None:
    latest = state.store.latest()
    if latest is not None:
        return latest
    try:
        return await asyncio.wait_for(
            state.store.wait_for_next(),
            timeout=state.first_frame_timeout,
        )
    except TimeoutError:
        return None


async def snapshot_handler(request: web.Request) -> web.Response:
    state: CameraServiceState = request.app["state"]
    if not await state.ensure_started():
        return web.Response(status=503, text=f"camera unavailable: {state.error}\n")
    jpeg = await wait_for_frame(state)
    state.schedule_idle_stop()
    if jpeg is None:
        return web.Response(status=503, text="no frame yet\n")
    return web.Response(
        body=jpeg,
        content_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


async def stream_handler(request: web.Request) -> web.StreamResponse:
    state: CameraServiceState = request.app["state"]
    state.acquire_stream()
    try:
        if not await state.ensure_started():
            return web.Response(status=503, text=f"camera unavailable: {state.error}\n")
        first_frame = await wait_for_frame(state)
        if first_frame is None:
            return web.Response(status=503, text="no frame yet\n")

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
                "Cache-Control": "no-store, private",
                "Pragma": "no-cache",
            },
        )
        await response.prepare(request)

        try:
            await response.write(mjpeg_part(first_frame))
            while True:
                jpeg = await state.store.wait_for_next()
                await response.write(mjpeg_part(jpeg))
        except (ConnectionResetError, ConnectionError):
            pass
        return response
    finally:
        state.release_stream()


def build_app(state: CameraServiceState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/health", health_handler)
    app.router.add_get("/snapshot.jpg", snapshot_handler)
    app.router.add_get("/stream.mjpg", stream_handler)
    return app


async def run_service(args: argparse.Namespace) -> None:
    loop = asyncio.get_running_loop()
    store = FrameStore(loop)
    state = CameraServiceState(
        store,
        driver_factory=lambda: CameraDriver(
            size=(args.width, args.height),
            jpeg_quality=args.quality,
            sensor_size=(args.sensor_width, args.sensor_height),
        ),
        fps=args.fps,
        idle_timeout=args.idle_timeout,
        loop=loop,
    )

    app = build_app(state)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()
    log.info("camera service listening on %s:%d", args.host, args.port)
    log.info(
        "camera idle start enabled size=%dx%d sensor=%dx%d fps=%.1f quality=%d idle_timeout=%.1fs",
        args.width,
        args.height,
        args.sensor_width,
        args.sensor_height,
        args.fps,
        args.quality,
        args.idle_timeout,
    )

    try:
        stop_event = asyncio.Event()
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
        loop.add_signal_handler(signal.SIGINT, stop_event.set)
        await stop_event.wait()
    finally:
        await runner.cleanup()
        state.stop_camera(force=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot camera HTTP service.")
    parser.add_argument("--host", default=DEFAULT_CAMERA_BIND_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_CAMERA_PORT)
    parser.add_argument("--width", type=int, default=DEFAULT_CAMERA_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_CAMERA_HEIGHT)
    parser.add_argument("--sensor-width", type=int, default=DEFAULT_SENSOR_WIDTH)
    parser.add_argument("--sensor-height", type=int, default=DEFAULT_SENSOR_HEIGHT)
    parser.add_argument("--fps", type=float, default=DEFAULT_CAPTURE_FPS)
    parser.add_argument("--quality", type=int, default=DEFAULT_JPEG_QUALITY)
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT_SECONDS)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run_service(args))


if __name__ == "__main__":
    main()
