#!/usr/bin/env python3
"""Camera service: owns the Pi camera and serves snapshots/MJPEG over HTTP.

Single owner of `drivers.camera.CameraDriver`. Other processes (web dashboard,
future perception) consume frames over HTTP rather than opening the camera.
"""

from __future__ import annotations

import argparse
import asyncio
import threading

from aiohttp import web

from drivers.camera import CameraDriver, CameraUnavailable
from lib.log import setup_logging
from telemetry.paths import DEFAULT_CAMERA_BIND_HOST, DEFAULT_CAMERA_PORT


DEFAULT_CAMERA_WIDTH = 320
DEFAULT_CAMERA_HEIGHT = 240
DEFAULT_CAPTURE_FPS = 10.0
DEFAULT_JPEG_QUALITY = 75
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

    def __init__(self, driver: CameraDriver, store: FrameStore, fps: float):
        self._driver = driver
        self._store = store
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
                self._stop.wait(0.5)
                continue
            self._store.publish(jpeg)
            if self._period > 0:
                self._stop.wait(self._period)


class CameraServiceState:
    """Shared state passed to aiohttp handlers via app['state']."""

    def __init__(self, store: FrameStore):
        self.store = store
        self.camera_ok: bool = False
        self.error: str | None = None


async def health_handler(request: web.Request) -> web.Response:
    state: CameraServiceState = request.app["state"]
    if not state.camera_ok:
        return web.json_response(
            {"status": "unavailable", "error": state.error},
            status=503,
        )
    return web.json_response(
        {"status": "ok", "has_frame": state.store.latest() is not None}
    )


async def snapshot_handler(request: web.Request) -> web.Response:
    state: CameraServiceState = request.app["state"]
    jpeg = state.store.latest()
    if jpeg is None:
        return web.Response(status=503, text="no frame yet\n")
    return web.Response(
        body=jpeg,
        content_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


async def stream_handler(request: web.Request) -> web.StreamResponse:
    state: CameraServiceState = request.app["state"]
    if state.store.latest() is None:
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

    while True:
        jpeg = await state.store.wait_for_next()
        try:
            await response.write(mjpeg_part(jpeg))
        except (ConnectionResetError, ConnectionError):
            break
    return response


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
    state = CameraServiceState(store)

    driver = CameraDriver(
        size=(args.width, args.height),
        jpeg_quality=args.quality,
    )
    capture: CameraCaptureThread | None = None
    try:
        driver.start()
    except CameraUnavailable as exc:
        log.error("camera unavailable: %s", exc)
        state.error = str(exc)
    else:
        state.camera_ok = True
        capture = CameraCaptureThread(driver, store, args.fps)
        capture.start()
        log.info(
            "camera started size=%dx%d fps=%.1f quality=%d",
            args.width,
            args.height,
            args.fps,
            args.quality,
        )

    app = build_app(state)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()
    log.info("camera service listening on %s:%d", args.host, args.port)

    try:
        await asyncio.Future()
    finally:
        await runner.cleanup()
        if capture is not None:
            capture.stop()
        if state.camera_ok:
            driver.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot camera HTTP service.")
    parser.add_argument("--host", default=DEFAULT_CAMERA_BIND_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_CAMERA_PORT)
    parser.add_argument("--width", type=int, default=DEFAULT_CAMERA_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_CAMERA_HEIGHT)
    parser.add_argument("--fps", type=float, default=DEFAULT_CAPTURE_FPS)
    parser.add_argument("--quality", type=int, default=DEFAULT_JPEG_QUALITY)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run_service(args))


if __name__ == "__main__":
    main()
