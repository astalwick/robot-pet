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
import time
from collections.abc import Callable
from dataclasses import dataclass

from aiohttp import web

from drivers.camera import CameraDriver, CameraUnavailable
from lib.log import setup_logging
from telemetry.paths import DEFAULT_CAMERA_BIND_HOST, DEFAULT_CAMERA_PORT


DEFAULT_CAMERA_WIDTH = 1280
DEFAULT_CAMERA_HEIGHT = 720
DEFAULT_SENSOR_WIDTH = 2304
DEFAULT_SENSOR_HEIGHT = 1296
DEFAULT_VISION_WIDTH = DEFAULT_CAMERA_WIDTH
DEFAULT_VISION_HEIGHT = DEFAULT_CAMERA_HEIGHT
DEFAULT_CAPTURE_FPS = 10.0
DEFAULT_JPEG_QUALITY = 75
DEFAULT_IDLE_TIMEOUT_SECONDS = 30.0
FIRST_FRAME_TIMEOUT_SECONDS = 3.0
SHUTDOWN_TIMEOUT_SECONDS = 2.0
MJPEG_BOUNDARY = "robotpet-frame"


log = setup_logging("robot-camera")


@dataclass(frozen=True)
class VisionFrame:
    data: bytes
    width: int
    height: int
    timestamp: float


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


class VisionFrameStore:
    """Thread-safe latest raw grayscale frame store."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._lock = threading.Lock()
        self._latest: VisionFrame | None = None
        self._waiters: list[asyncio.Event] = []

    def publish(self, data: bytes, width: int, height: int) -> None:
        frame = VisionFrame(data, width, height, time.monotonic())
        with self._lock:
            self._latest = frame
            waiters = self._waiters
            self._waiters = []
        for waiter in waiters:
            self._loop.call_soon_threadsafe(waiter.set)

    def latest(self) -> VisionFrame | None:
        with self._lock:
            return self._latest

    def clear(self) -> None:
        with self._lock:
            self._latest = None

    async def wait_for_next(self) -> VisionFrame:
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


class CameraServiceState:
    """Owns on-demand camera runtime state for aiohttp handlers."""

    def __init__(
        self,
        store: FrameStore,
        *,
        vision_store: VisionFrameStore | None = None,
        driver_factory: Callable[[], CameraDriver] | None = None,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
        first_frame_timeout: float = FIRST_FRAME_TIMEOUT_SECONDS,
        loop: asyncio.AbstractEventLoop | None = None,
    ):
        self.store = store
        self.vision_store = vision_store
        self.camera_ok: bool = False
        self.error: str | None = None
        self.active_streams = 0

        self._driver_factory = driver_factory
        self._idle_timeout = idle_timeout
        self.first_frame_timeout = first_frame_timeout
        self._loop = loop
        self._driver: CameraDriver | None = None
        self._idle_handle: asyncio.TimerHandle | None = None
        self._lock = threading.Lock()
        self._driver_lifecycle_lock = threading.Lock()
        self._start_lock = asyncio.Lock()

    async def ensure_started(self) -> bool:
        """Start the camera if this service owns a driver and it is idle."""
        async with self._start_lock:
            self._cancel_idle_stop()
            with self._lock:
                if self._driver is not None:
                    return True
                if self._driver_factory is None:
                    return self.camera_ok or self.store.latest() is not None

            self.store.clear()
            if self.vision_store is not None:
                self.vision_store.clear()
            try:
                # picamera2 configure/start can take the better part of a second;
                # keep it off the event loop so /health and other handlers stay live.
                await asyncio.to_thread(self._start_camera)
            except CameraUnavailable as exc:
                log.error("camera unavailable: %s", exc)
                with self._lock:
                    self.camera_ok = False
                    self.error = str(exc)
                return False

            return True

    def _start_camera(self) -> None:
        with self._driver_lifecycle_lock:
            driver = self._driver_factory()
            driver.start(
                self.store.publish,
                self.vision_store.publish if self.vision_store is not None else None,
            )
            with self._lock:
                self._driver = driver
                self.camera_ok = True
                self.error = None
            log.info("camera became active")

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
        self._idle_handle = loop.call_later(self._idle_timeout, self._idle_stop)

    def _idle_stop(self) -> None:
        # driver.stop() blocks; run it off the event loop. Shutdown still calls
        # stop_camera() directly so the camera is released before exit.
        threading.Thread(target=self.stop_camera, name="camera-idle-stop", daemon=True).start()

    def stop_camera(self, *, force: bool = False) -> None:
        with self._driver_lifecycle_lock:
            with self._lock:
                if self.active_streams > 0 and not force:
                    return
                driver = self._driver
                self._driver = None
                self.camera_ok = False
                self._idle_handle = None
            if driver is not None:
                driver.stop()
            self.store.clear()
            if self.vision_store is not None:
                self.vision_store.clear()
            if driver is not None:
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


async def wait_for_vision_frame(state: CameraServiceState) -> VisionFrame | None:
    if state.vision_store is None:
        return None
    latest = state.vision_store.latest()
    if latest is not None:
        return latest
    try:
        return await asyncio.wait_for(
            state.vision_store.wait_for_next(),
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


async def vision_gray_handler(request: web.Request) -> web.Response:
    state: CameraServiceState = request.app["state"]
    if not await state.ensure_started():
        return web.Response(status=503, text=f"camera unavailable: {state.error}\n")
    frame = await wait_for_vision_frame(state)
    state.schedule_idle_stop()
    if frame is None:
        return web.Response(status=503, text="no vision frame yet\n")
    return web.Response(
        body=frame.data,
        content_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Width": str(frame.width),
            "X-Frame-Height": str(frame.height),
            "X-Frame-Format": "gray8",
            "X-Frame-Time": f"{frame.timestamp:.6f}",
        },
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
    app.router.add_get("/vision.gray", vision_gray_handler)
    app.router.add_get("/stream.mjpg", stream_handler)
    return app


async def run_service(args: argparse.Namespace) -> None:
    loop = asyncio.get_running_loop()
    store = FrameStore(loop)
    vision_store = VisionFrameStore(loop)
    state = CameraServiceState(
        store,
        vision_store=vision_store,
        driver_factory=lambda: CameraDriver(
            size=(args.width, args.height),
            jpeg_quality=args.quality,
            sensor_size=(args.sensor_width, args.sensor_height),
            vision_size=(args.vision_width, args.vision_height),
            fps=args.fps,
        ),
        idle_timeout=args.idle_timeout,
        loop=loop,
    )

    app = build_app(state)
    runner = web.AppRunner(app, shutdown_timeout=SHUTDOWN_TIMEOUT_SECONDS)
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
    parser.add_argument("--vision-width", type=int, default=DEFAULT_VISION_WIDTH)
    parser.add_argument("--vision-height", type=int, default=DEFAULT_VISION_HEIGHT)
    parser.add_argument("--fps", type=float, default=DEFAULT_CAPTURE_FPS)
    parser.add_argument("--quality", type=int, default=DEFAULT_JPEG_QUALITY)
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT_SECONDS)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run_service(args))


if __name__ == "__main__":
    main()
