#!/usr/bin/env python3
"""Web dashboard service: serves operator UI, telemetry, logs, and actions.

Subscribes to the existing telemetry hub on a background thread and fans out the
latest snapshot to browser clients via Server-Sent Events.
The browser builds the camera URL from `location.hostname`, so a remote MacBook
loads MJPEG from the Pi's camera service rather than its own loopback.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from config.teleop import (
    DEFAULT_CONFIG_PATH,
    DriveTuning,
    DriveTuningConfigError,
    load_drive_tuning,
    save_drive_tuning,
)
from lib.log import setup_logging
from telemetry.paths import (
    DEFAULT_SUBSCRIBE_SOCKET,
    DEFAULT_WEB_DASHBOARD_HOST,
    DEFAULT_WEB_DASHBOARD_PORT,
)
from telemetry.socket_client import subscribe


STATIC_DIR = Path(__file__).resolve().parent / "web_dashboard_static"
REDEPLOY_ARM_SECONDS = 10.0

# Keep aligned with `systemctl enable` in setup.sh (all shipped robot *.service units).
LOG_COMMAND = [
    "journalctl",
    "-u",
    "robot-telemetry",
    "-u",
    "gamepad-teleop",
    "-u",
    "robot-brain",
    "-u",
    "robot-camera",
    "-u",
    "robot-web-dashboard",
    "-f",
    "-n",
    "100",
]

TUNING_FIELDS = (
    ("speed_scale", "Normal speed", "0.25 = 25%"),
    ("turbo_scale", "Turbo speed", "0.75 = 75%"),
    ("turn_scale", "Turn scale", "1.0 = full turn"),
    ("left_stick_deadzone", "Left stick deadzone", "0.15"),
    ("right_stick_deadzone", "Right stick deadzone", "0.15"),
    ("qpps_slew_limit", "QPPS slew", "encoder counts/sec/sec"),
)

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


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


class BroadcastHub:
    """Thread-safe line fanout for operator action output."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._lock = threading.Lock()
        self._queues: list[asyncio.Queue[dict[str, str]]] = []

    def subscribe(self) -> asyncio.Queue[dict[str, str]]:
        queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        with self._lock:
            self._queues.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, str]]) -> None:
        with self._lock:
            if queue in self._queues:
                self._queues.remove(queue)

    def publish(self, line: str, source: str = "action") -> None:
        event = {"line": line, "source": source}
        with self._lock:
            queues = list(self._queues)
        for queue in queues:
            self._loop.call_soon_threadsafe(queue.put_nowait, event)


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


def format_sse_json_event(payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, separators=(",", ":"))
    return f"data: {data}\n\n".encode("utf-8")


@web.middleware
async def no_cache_middleware(request: web.Request, handler: Callable[[web.Request], Any]) -> web.StreamResponse:
    response = await handler(request)
    response.headers.update(NO_CACHE_HEADERS)
    return response


def stream_command_output(
    command: list[str],
    on_line: Callable[[str], None],
    *,
    env: dict[str, str] | None = None,
) -> int:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    if process.stdout is not None:
        for line in process.stdout:
            on_line(line.rstrip())
    return process.wait()


def restart_gamepad_teleop() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", "systemctl", "restart", "gamepad-teleop.service"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def redeploy_command() -> tuple[list[str], dict[str, str]]:
    repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    script = os.path.join(repo_dir, "scripts", "redeploy-robot.sh")
    return [script], {**os.environ, "ROBOT_PET_REPO_DIR": repo_dir}


class WebDashboardState:
    """Shared state passed to aiohttp handlers via app['state']."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        snapshot_store: SnapshotStore,
        static_dir: Path,
        teleop_config_path: str,
    ):
        self.loop = loop
        self.snapshot_store = snapshot_store
        self.static_dir = static_dir
        self.teleop_config_path = teleop_config_path
        self.log_hub = BroadcastHub(loop)
        self._lock = threading.Lock()
        self.redeploy_armed_until = 0.0
        self.redeploy_running = False
        self.tuning_apply_running = False

    def redeploy_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "armed": time.monotonic() <= self.redeploy_armed_until,
                "armed_seconds_remaining": max(0.0, self.redeploy_armed_until - time.monotonic()),
                "running": self.redeploy_running,
            }

    def arm_redeploy(self) -> dict[str, Any]:
        with self._lock:
            if not self.redeploy_running:
                self.redeploy_armed_until = time.monotonic() + REDEPLOY_ARM_SECONDS
            return {
                "armed": time.monotonic() <= self.redeploy_armed_until,
                "armed_seconds_remaining": max(0.0, self.redeploy_armed_until - time.monotonic()),
                "running": self.redeploy_running,
            }

    def start_redeploy(self) -> tuple[bool, dict[str, Any]]:
        with self._lock:
            now = time.monotonic()
            status = {
                "armed": now <= self.redeploy_armed_until,
                "armed_seconds_remaining": max(0.0, self.redeploy_armed_until - now),
                "running": self.redeploy_running,
            }
            if self.redeploy_running or now > self.redeploy_armed_until:
                return False, status
            self.redeploy_running = True
            self.redeploy_armed_until = 0.0
        threading.Thread(target=self._redeploy_thread, daemon=True).start()
        return True, self.redeploy_status()

    def set_tuning_apply_running(self, running: bool) -> None:
        with self._lock:
            self.tuning_apply_running = running

    def get_tuning_apply_running(self) -> bool:
        with self._lock:
            return self.tuning_apply_running

    def _redeploy_thread(self) -> None:
        self.log_hub.publish("Starting redeploy...")
        command, env = redeploy_command()
        try:
            exit_code = stream_command_output(command, self.log_hub.publish, env=env)
        except OSError as exc:
            with self._lock:
                self.redeploy_running = False
            self.log_hub.publish(f"Redeploy failed to start: {exc}")
            return

        with self._lock:
            self.redeploy_running = False
        if exit_code == 0:
            self.log_hub.publish("Redeploy succeeded.")
        else:
            self.log_hub.publish(f"Redeploy failed with exit code {exit_code}. Dashboard left running.")


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


async def logs_handler(request: web.Request) -> web.StreamResponse:
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

    action_queue = state.log_hub.subscribe()
    process: asyncio.subprocess.Process | None = None
    read_task: asyncio.Task[bytes] | None = None
    action_task: asyncio.Task[dict[str, str]] | None = None

    try:
        process = await asyncio.create_subprocess_exec(
            *LOG_COMMAND,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout is not None
        read_task = asyncio.create_task(process.stdout.readline())
        action_task = asyncio.create_task(action_queue.get())

        while True:
            done, _pending = await asyncio.wait(
                {read_task, action_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if read_task in done:
                line_bytes = read_task.result()
                if not line_bytes:
                    await response.write(
                        format_sse_json_event(
                            {"line": "journalctl exited; logs are no longer streaming.", "source": "journal"}
                        )
                    )
                    break
                line = line_bytes.decode("utf-8", errors="replace").rstrip()
                await response.write(format_sse_json_event({"line": line, "source": "journal"}))
                read_task = asyncio.create_task(process.stdout.readline())
            if action_task in done:
                await response.write(format_sse_json_event(action_task.result()))
                action_task = asyncio.create_task(action_queue.get())
    except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
        pass
    except OSError as exc:
        try:
            await response.write(format_sse_json_event({"line": f"journalctl unavailable: {exc}", "source": "journal"}))
        except (ConnectionResetError, ConnectionError):
            pass
    finally:
        state.log_hub.unsubscribe(action_queue)
        for task in (read_task, action_task):
            if task is not None:
                task.cancel()
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
    return response


async def redeploy_status_handler(request: web.Request) -> web.Response:
    state: WebDashboardState = request.app["state"]
    return web.json_response(state.redeploy_status())


async def redeploy_arm_handler(request: web.Request) -> web.Response:
    state: WebDashboardState = request.app["state"]
    status = state.arm_redeploy()
    if status["running"]:
        state.log_hub.publish("Redeploy already running.")
    else:
        state.log_hub.publish("Redeploy armed. Press redeploy again within 10s to git fast-forward and restart robot services.")
    return web.json_response(status)


async def redeploy_run_handler(request: web.Request) -> web.Response:
    state: WebDashboardState = request.app["state"]
    started, status = state.start_redeploy()
    if not started:
        if status["running"]:
            state.log_hub.publish("Redeploy already running.")
        else:
            state.log_hub.publish("Redeploy is not armed. Press redeploy once to arm it first.")
        return web.json_response(status, status=409)
    return web.json_response(status)


def drive_tuning_payload(tuning: DriveTuning) -> dict[str, Any]:
    return {
        "values": tuning.to_dict(),
        "fields": [
            {"key": key, "label": label, "help": help_text}
            for key, label, help_text in TUNING_FIELDS
        ],
    }


async def drive_config_handler(request: web.Request) -> web.Response:
    state: WebDashboardState = request.app["state"]
    try:
        tuning = load_drive_tuning(state.teleop_config_path)
    except DriveTuningConfigError as exc:
        return web.json_response(
            {
                **drive_tuning_payload(DriveTuning()),
                "error": str(exc),
            },
            status=200,
        )
    return web.json_response(drive_tuning_payload(tuning))


async def drive_config_apply_handler(request: web.Request) -> web.Response:
    state: WebDashboardState = request.app["state"]
    if state.get_tuning_apply_running():
        return web.json_response({"error": "Drive tuning apply already running."}, status=409)

    try:
        values = await request.json()
        if not isinstance(values, dict):
            raise ValueError("expected a JSON object")
        tuning = DriveTuning.from_dict(values)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": f"Invalid drive tuning: {exc}"}, status=400)

    state.set_tuning_apply_running(True)
    state.log_hub.publish("Saving drive tuning and restarting gamepad-teleop...")
    try:
        await asyncio.to_thread(save_drive_tuning, tuning, state.teleop_config_path)
        result = await asyncio.to_thread(restart_gamepad_teleop)
    except Exception as exc:
        state.log_hub.publish(f"Drive tuning apply failed: {exc}")
        return web.json_response({"error": str(exc)}, status=500)
    finally:
        state.set_tuning_apply_running(False)

    if result.returncode == 0:
        state.log_hub.publish("Drive tuning saved. gamepad-teleop restarted.")
        return web.json_response({"ok": True, **drive_tuning_payload(tuning)})

    output = (result.stderr or result.stdout).strip()
    state.log_hub.publish(f"Drive tuning saved, but restart failed: {output}")
    return web.json_response({"error": output, **drive_tuning_payload(tuning)}, status=500)


def build_app(state: WebDashboardState) -> web.Application:
    app = web.Application(middlewares=[no_cache_middleware])
    app["state"] = state
    app.router.add_get("/", index_handler)
    app.router.add_get("/events", events_handler)
    app.router.add_get("/logs/events", logs_handler)
    app.router.add_get("/redeploy/status", redeploy_status_handler)
    app.router.add_post("/redeploy/arm", redeploy_arm_handler)
    app.router.add_post("/redeploy/run", redeploy_run_handler)
    app.router.add_get("/config/drive", drive_config_handler)
    app.router.add_post("/config/drive", drive_config_apply_handler)
    app.router.add_static("/static", str(state.static_dir), show_index=False)
    return app


async def run_service(args: argparse.Namespace) -> None:
    loop = asyncio.get_running_loop()
    snapshot_store = SnapshotStore(loop)
    state = WebDashboardState(loop, snapshot_store, Path(args.static_dir), args.teleop_config)

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
        stop_event = asyncio.Event()
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
        loop.add_signal_handler(signal.SIGINT, stop_event.set)
        await stop_event.wait()
    finally:
        await runner.cleanup()
        subscriber.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot web dashboard service.")
    parser.add_argument("--host", default=DEFAULT_WEB_DASHBOARD_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_DASHBOARD_PORT)
    parser.add_argument("--telemetry-socket", default=DEFAULT_SUBSCRIBE_SOCKET)
    parser.add_argument("--teleop-config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--static-dir", default=str(STATIC_DIR))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run_service(args))


if __name__ == "__main__":
    main()
