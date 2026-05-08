#!/usr/bin/env python3
"""Web dashboard service: serves operator UI and an SSE telemetry stream.

Read-only first cut. Subscribes to the existing telemetry hub on a background
thread, fans out the latest snapshot to browser clients via Server-Sent Events.
The browser builds the camera URL from `location.hostname`, so a remote MacBook
loads MJPEG from the Pi's camera service rather than its own loopback.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from aiohttp import web

from lib.log import setup_logging
from telemetry.paths import (
    DEFAULT_SUBSCRIBE_SOCKET,
    DEFAULT_WEB_DASHBOARD_HOST,
    DEFAULT_WEB_DASHBOARD_PORT,
)
from telemetry.socket_client import subscribe


STATIC_DIR = Path(__file__).resolve().parent / "web_dashboard_static"


log = setup_logging("robot-web-dashboard")


class SnapshotStore:
    """Thread-safe latest-snapshot store with async new-snapshot notifications.

    The subscriber thread calls `publish` for every snapshot received from
    the telemetry hub. SSE handlers call `latest` for the initial frame and
    await `wait_for_next` for subsequent updates. Slow SSE clients cannot
    block the subscriber thread or other clients.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._lock = threading.Lock()
        self._latest: dict[str, Any] | None = None
        self._waiters: list[asyncio.Event] = []

    def publish(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._latest = snapshot
            waiters = self._waiters
            self._waiters = []
        for waiter in waiters:
            self._loop.call_soon_threadsafe(waiter.set)

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            return self._latest

    async def wait_for_next(self) -> dict[str, Any]:
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


class TelemetrySubscriberThread:
    """Runs the blocking telemetry subscribe iterator and publishes snapshots."""

    def __init__(self, store: SnapshotStore, socket_path: str):
        self._store = store
        self._socket_path = socket_path
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="telemetry-subscribe", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        for snapshot in subscribe(self._socket_path, reconnect_interval=1.0):
            if self._stop.is_set():
                break
            self._store.publish(snapshot)


def format_sse_event(snapshot: dict[str, Any]) -> bytes:
    """Encode a snapshot as a single SSE `data:` event."""
    payload = json.dumps(snapshot, separators=(",", ":"))
    return f"data: {payload}\n\n".encode("utf-8")


class WebDashboardState:
    """Shared state passed to aiohttp handlers via app['state']."""

    def __init__(self, snapshot_store: SnapshotStore, static_dir: Path):
        self.snapshot_store = snapshot_store
        self.static_dir = static_dir


async def index_handler(request: web.Request) -> web.FileResponse:
    state: WebDashboardState = request.app["state"]
    return web.FileResponse(state.static_dir / "index.html")


async def events_handler(request: web.Request) -> web.StreamResponse:
    state: WebDashboardState = request.app["state"]
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    initial = state.snapshot_store.latest()
    if initial is not None:
        try:
            await response.write(format_sse_event(initial))
        except (ConnectionResetError, ConnectionError):
            return response

    while True:
        snapshot = await state.snapshot_store.wait_for_next()
        try:
            await response.write(format_sse_event(snapshot))
        except (ConnectionResetError, ConnectionError):
            break
    return response


def build_app(state: WebDashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/", index_handler)
    app.router.add_get("/events", events_handler)
    app.router.add_static("/static", str(state.static_dir), show_index=False)
    return app


async def run_service(args: argparse.Namespace) -> None:
    loop = asyncio.get_running_loop()
    snapshot_store = SnapshotStore(loop)
    state = WebDashboardState(snapshot_store, Path(args.static_dir))

    subscriber = TelemetrySubscriberThread(snapshot_store, args.telemetry_socket)
    subscriber.start()

    app = build_app(state)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()
    log.info("web dashboard listening on %s:%d", args.host, args.port)
    log.info("subscribing to telemetry at %s", args.telemetry_socket)

    try:
        await asyncio.Future()
    finally:
        await runner.cleanup()
        subscriber.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot web dashboard service.")
    parser.add_argument("--host", default=DEFAULT_WEB_DASHBOARD_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_DASHBOARD_PORT)
    parser.add_argument("--telemetry-socket", default=DEFAULT_SUBSCRIBE_SOCKET)
    parser.add_argument("--static-dir", default=str(STATIC_DIR))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run_service(args))


if __name__ == "__main__":
    main()
