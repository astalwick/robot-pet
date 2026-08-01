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
import re
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from config.drive_tuning import (
    DEFAULT_CONFIG_PATH,
    TUNING_FIELDS,
    DriveTuning,
    DriveTuningConfigError,
    load_drive_tuning,
    save_drive_tuning,
)
from config.vision import (
    DEFAULT_CONFIG_PATH as DEFAULT_VISION_CONFIG_PATH,
    VISION_FIELDS,
    VisionConfig,
    VisionConfigError,
    load_vision_config,
    save_vision_config,
)
from config.sensors import (
    DEFAULT_CONFIG_PATH as DEFAULT_SENSORS_CONFIG_PATH,
    SENSORS_FIELDS,
    SensorsConfig,
    SensorsConfigError,
    load_sensors_config,
    save_sensors_config,
)
from config.voice import (
    DEFAULT_CONFIG_PATH as DEFAULT_VOICE_CONFIG_PATH,
    VOICE_FIELDS,
    VoiceConfig,
    VoiceConfigError,
    load_voice_config,
    save_voice_config,
)
from lib.log import setup_logging
from telemetry.paths import (
    DEFAULT_SUBSCRIBE_SOCKET,
    DEFAULT_VOICE_COMMAND_SOCKET,
    DEFAULT_WEB_DASHBOARD_HOST,
    DEFAULT_WEB_DASHBOARD_PORT,
)
from telemetry.socket_client import publish_message, send_voice_command, subscribe
from voice import model_frames
from voice.personality import load_personalities


STATIC_DIR = Path(__file__).resolve().parent / "web_dashboard_static"
REDEPLOY_ARM_SECONDS = 10.0
DEFAULT_REDEPLOY_STATUS_PATH = "/tmp/robot-pet-web-dashboard-redeploy.json"
SHUTDOWN_TIMEOUT_SECONDS = 2.0

# Keep aligned with `systemctl enable` in setup.sh (all shipped robot *.service units).
LOG_COMMAND = [
    "journalctl",
    "-u",
    "robot-telemetry",
    "-u",
    "robot-battery",
    "-u",
    "robot-pi-battery",
    "-u",
    "robot-motion",
    "-u",
    "gamepad-teleop",
    "-u",
    "robot-brain",
    "-u",
    "robot-camera",
    "-u",
    "robot-vision",
    "-u",
    "robot-sensors",
    "-u",
    "robot-voice",
    "-u",
    "robot-web-dashboard",
    "-f",
    "-n",
    "100",
]

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


JOURNAL_BACKLOG_LINES = 200
JOURNAL_RESTART_DELAY_SECONDS = 1.0


class JournalFollower:
    """One shared `journalctl -f` process feeding the log hub.

    Spawning a follower per browser tab meant several journalctl processes
    tailing eleven units each; this starts a single one on first use and
    replays a backlog of recent lines to newly connected tabs.
    """

    def __init__(self, hub: BroadcastHub):
        self.hub = hub
        self.backlog: deque[str] = deque(maxlen=JOURNAL_BACKLOG_LINES)
        self._task: asyncio.Task | None = None
        self._process: asyncio.subprocess.Process | None = None

    def ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._follow())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self._process.kill()

    async def _follow(self) -> None:
        while True:
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *LOG_COMMAND,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except OSError as exc:
                self.hub.publish(f"journalctl unavailable: {exc}; retrying.", source="journal")
                await asyncio.sleep(JOURNAL_RESTART_DELAY_SECONDS)
                continue
            assert self._process.stdout is not None
            while line_bytes := await self._process.stdout.readline():
                line = line_bytes.decode("utf-8", errors="replace").rstrip()
                self.backlog.append(line)
                self.hub.publish(line, source="journal")
            self._process = None
            self.hub.publish("journalctl exited; restarting log stream.", source="journal")
            await asyncio.sleep(JOURNAL_RESTART_DELAY_SECONDS)


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


def restart_robot_sensors() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", "systemctl", "restart", "robot-sensors.service"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def restart_robot_motion() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", "systemctl", "restart", "robot-motion.service"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def restart_web_dashboard() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", "systemctl", "restart", "--no-block", "robot-web-dashboard.service"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def redeploy_command() -> tuple[list[str], dict[str, str]]:
    repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    script = os.path.join(repo_dir, "scripts", "redeploy-robot.sh")
    return [script], {
        **os.environ,
        "ROBOT_PET_REPO_DIR": repo_dir,
        "ROBOT_PET_REDEPLOY_STATUS_FILE": DEFAULT_REDEPLOY_STATUS_PATH,
    }


class WebDashboardState:
    """Shared state passed to aiohttp handlers via app['state']."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        snapshot_store: SnapshotStore,
        static_dir: Path,
        drive_tuning_config_path: str,
        vision_config_path: str,
        voice_config_path: str,
        voice_command_socket: str,
        sensors_config_path: str = DEFAULT_SENSORS_CONFIG_PATH,
        redeploy_status_path: str = DEFAULT_REDEPLOY_STATUS_PATH,
    ):
        self.loop = loop
        self.snapshot_store = snapshot_store
        self.static_dir = static_dir
        self.drive_tuning_config_path = drive_tuning_config_path
        self.vision_config_path = vision_config_path
        self.voice_config_path = voice_config_path
        self.voice_command_socket = voice_command_socket
        self.sensors_config_path = sensors_config_path
        self.redeploy_status_path = redeploy_status_path
        self.log_hub = BroadcastHub(loop)
        self.journal_follower = JournalFollower(self.log_hub)
        self._lock = threading.Lock()
        self.redeploy_armed_until = 0.0
        self.redeploy_running = False
        self.redeploy_last_result: str | None = None
        self.redeploy_last_message = ""
        self.redeploy_result_serial = 0
        self.tuning_apply_running = False

    def _redeploy_status_locked(self) -> dict[str, Any]:
        status_path = Path(self.redeploy_status_path)
        try:
            stat = status_path.stat()
            payload = json.loads(status_path.read_text())
        except FileNotFoundError:
            payload = None
        except (OSError, json.JSONDecodeError, TypeError):
            payload = None
        if (
            payload is not None
            and payload.get("last_result") in {"success", "failed"}
            and stat.st_mtime_ns > self.redeploy_result_serial
        ):
            self.redeploy_running = False
            self.redeploy_result_serial = stat.st_mtime_ns
            self.redeploy_last_result = payload["last_result"]
            self.redeploy_last_message = str(payload.get("last_message") or "")

        now = time.monotonic()
        return {
            "armed": now <= self.redeploy_armed_until,
            "armed_seconds_remaining": max(0.0, self.redeploy_armed_until - now),
            "running": self.redeploy_running,
            "last_result": self.redeploy_last_result,
            "last_message": self.redeploy_last_message,
            "result_serial": self.redeploy_result_serial,
        }

    def redeploy_status(self) -> dict[str, Any]:
        with self._lock:
            return self._redeploy_status_locked()

    def arm_redeploy(self) -> dict[str, Any]:
        with self._lock:
            if not self.redeploy_running:
                self.redeploy_armed_until = time.monotonic() + REDEPLOY_ARM_SECONDS
            return self._redeploy_status_locked()

    def start_redeploy(self) -> tuple[bool, dict[str, Any]]:
        with self._lock:
            status = self._redeploy_status_locked()
            if self.redeploy_running or time.monotonic() > self.redeploy_armed_until:
                return False, status
            self.redeploy_running = True
            self.redeploy_armed_until = 0.0
            self.redeploy_last_result = None
            self.redeploy_last_message = ""
            try:
                Path(self.redeploy_status_path).unlink()
            except FileNotFoundError:
                pass
        threading.Thread(target=self._redeploy_thread, daemon=True).start()
        return True, self.redeploy_status()

    def _finish_redeploy(self, result: str, message: str) -> None:
        try:
            status_path = Path(self.redeploy_status_path)
            status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = status_path.with_name(f".{status_path.name}.{os.getpid()}.tmp")
            tmp_path.write_text(json.dumps({"last_result": result, "last_message": message}) + "\n")
            os.replace(tmp_path, status_path)
            result_serial = status_path.stat().st_mtime_ns
        except OSError:
            result_serial = time.time_ns()
        with self._lock:
            self.redeploy_running = False
            self.redeploy_result_serial = result_serial
            self.redeploy_last_result = result
            self.redeploy_last_message = message

    def set_tuning_apply_running(self, running: bool) -> None:
        with self._lock:
            self.tuning_apply_running = running

    def get_tuning_apply_running(self) -> bool:
        with self._lock:
            return self.tuning_apply_running

    def _read_redeploy_status_file(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(Path(self.redeploy_status_path).read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _redeploy_thread(self) -> None:
        self.log_hub.publish("Starting redeploy...")
        command, env = redeploy_command()
        last_line = ""

        def on_line(line: str) -> None:
            nonlocal last_line
            if line:
                last_line = line
            self.log_hub.publish(line)

        try:
            exit_code = stream_command_output(command, on_line, env=env)
        except OSError as exc:
            message = f"Redeploy failed to start: {exc}"
            self.log_hub.publish(message)
            self._finish_redeploy("failed", message)
            return

        script_status = self._read_redeploy_status_file()
        if exit_code == 0 or (script_status and script_status.get("last_result") == "success"):
            message = str(script_status.get("last_message") if script_status else "") or last_line or "Redeploy complete."
            self.log_hub.publish("Redeploy succeeded.")
            if script_status and script_status.get("restart_dashboard"):
                self.log_hub.publish("restarting robot-web-dashboard.service")
                try:
                    result = restart_web_dashboard()
                except subprocess.TimeoutExpired:
                    self._finish_redeploy("failed", "Redeploy complete, but dashboard restart timed out.")
                    return
                if result.returncode != 0:
                    output = (result.stderr or result.stdout).strip()
                    self._finish_redeploy("failed", f"Redeploy complete, but dashboard restart failed: {output}")
                    return
            self._finish_redeploy("success", message)
            return

        message = last_line or f"Redeploy failed with exit code {exit_code}."
        self.log_hub.publish(f"Redeploy failed with exit code {exit_code}. Dashboard left running.")
        self._finish_redeploy("failed", message)


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

    queue = state.log_hub.subscribe()
    state.journal_follower.ensure_started()

    try:
        for line in list(state.journal_follower.backlog):
            await response.write(format_sse_json_event({"line": line, "source": "journal"}))
        while True:
            await response.write(format_sse_json_event(await queue.get()))
    except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
        pass
    finally:
        state.log_hub.unsubscribe(queue)
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


def vision_config_payload(config: VisionConfig) -> dict[str, Any]:
    return {
        "values": config.to_dict(),
        "fields": [dict(field) for field in VISION_FIELDS],
    }


def voice_config_payload(config: VoiceConfig) -> dict[str, Any]:
    return {
        "values": config.to_dict(),
        "fields": [dict(field) for field in VOICE_FIELDS],
    }


def sensors_form_values(config: SensorsConfig) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "poll_rate_hz": config.poll_rate_hz,
        "safety_enabled": config.safety.enabled,
        "cliff_trip_above_mm": config.safety.cliff_trip_above_mm,
        "forward_stop_below_mm": config.safety.forward_stop_below_mm,
    }


def sensors_config_payload(config: SensorsConfig) -> dict[str, Any]:
    return {
        "values": sensors_form_values(config),
        "fields": [dict(field) for field in SENSORS_FIELDS],
    }


def merge_sensors_form_patch(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    if "enabled" in patch:
        merged["enabled"] = bool(patch["enabled"])
    if "poll_rate_hz" in patch:
        merged["poll_rate_hz"] = patch["poll_rate_hz"]
    safety = dict(merged.get("safety", {}))
    if "safety_enabled" in patch:
        safety["enabled"] = bool(patch["safety_enabled"])
    if "cliff_trip_above_mm" in patch:
        safety["cliff_trip_above_mm"] = int(patch["cliff_trip_above_mm"])
    if "forward_stop_below_mm" in patch:
        safety["forward_stop_below_mm"] = int(patch["forward_stop_below_mm"])
    merged["safety"] = safety
    return merged


CONFIGS: dict[str, dict[str, Any]] = {
    "drive": {
        "path_attr": "drive_tuning_config_path",
        "load": load_drive_tuning,
        "save": save_drive_tuning,
        "from_dict": DriveTuning.from_dict,
        "default": DriveTuning,
        "error": DriveTuningConfigError,
        "payload": drive_tuning_payload,
        "saved_log": "Drive tuning saved.",
    },
    "vision": {
        "path_attr": "vision_config_path",
        "load": load_vision_config,
        "save": save_vision_config,
        "from_dict": VisionConfig.from_dict,
        "default": VisionConfig,
        "error": VisionConfigError,
        "payload": vision_config_payload,
        "saved_log": "Vision config saved.",
    },
    "voice": {
        "path_attr": "voice_config_path",
        "load": load_voice_config,
        "save": save_voice_config,
        "from_dict": VoiceConfig.from_dict,
        "default": VoiceConfig,
        "error": VoiceConfigError,
        "payload": voice_config_payload,
        "saved_log": "Voice config saved.",
    },
    "sensors": {
        "path_attr": "sensors_config_path",
        "load": load_sensors_config,
        "save": save_sensors_config,
        "from_dict": SensorsConfig.from_dict,
        "default": SensorsConfig,
        "error": SensorsConfigError,
        "payload": sensors_config_payload,
        "saved_log": "Sensors config saved.",
    },
}


def _config_path(state: WebDashboardState, name: str) -> str:
    return getattr(state, CONFIGS[name]["path_attr"])


async def config_get(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    spec = CONFIGS.get(name)
    if spec is None:
        raise web.HTTPNotFound()

    state: WebDashboardState = request.app["state"]
    path = _config_path(state, name)
    try:
        config = await asyncio.to_thread(spec["load"], path)
    except spec["error"] as exc:
        return web.json_response(
            {
                **spec["payload"](spec["default"]()),
                "error": str(exc),
            },
            status=200,
        )
    return web.json_response(spec["payload"](config))


async def config_apply(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    spec = CONFIGS.get(name)
    if spec is None:
        raise web.HTTPNotFound()

    state: WebDashboardState = request.app["state"]
    path = _config_path(state, name)

    try:
        patch = await request.json()
        if not isinstance(patch, dict):
            raise ValueError("expected a JSON object")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": f"Invalid {name} config: {exc}"}, status=400)
    if name == "voice":
        if "wake_word_enabled" in patch:
            patch["enabled"] = bool(patch["wake_word_enabled"])
        elif "enabled" in patch:
            patch["wake_word_enabled"] = bool(patch["enabled"])

    try:
        current = await asyncio.to_thread(spec["load"], path)
    except spec["error"]:
        current = spec["default"]()

    merge_base = current.to_dict()
    try:
        if name == "sensors":
            merge_base = merge_sensors_form_patch(merge_base, patch)
            patch = {}
        config = spec["from_dict"]({**merge_base, **patch})
    except (TypeError, ValueError) as exc:
        return web.json_response({"error": f"Invalid {name} config: {exc}"}, status=400)

    if name == "drive":
        if state.get_tuning_apply_running():
            return web.json_response({"error": "Drive tuning apply already running."}, status=409)

        state.set_tuning_apply_running(True)
        # robot-motion now reads the slew limit from this config at startup, so it
        # must restart alongside gamepad-teleop for a change to take effect.
        state.log_hub.publish("Saving drive tuning and restarting gamepad-teleop and robot-motion...")
        try:
            await asyncio.to_thread(spec["save"], config, path)
            teleop_result = await asyncio.to_thread(restart_gamepad_teleop)
            motion_result = await asyncio.to_thread(restart_robot_motion)
        except Exception as exc:
            state.log_hub.publish(f"Drive tuning apply failed: {exc}")
            return web.json_response({"error": str(exc)}, status=500)
        finally:
            state.set_tuning_apply_running(False)

        if teleop_result.returncode != 0:
            output = (teleop_result.stderr or teleop_result.stdout).strip()
            state.log_hub.publish(f"Drive tuning saved, but gamepad-teleop restart failed: {output}")
            return web.json_response({"error": output, **spec["payload"](config)}, status=500)
        if motion_result.returncode != 0:
            output = (motion_result.stderr or motion_result.stdout).strip()
            state.log_hub.publish(f"Drive tuning saved, but robot-motion restart failed: {output}")
            return web.json_response({"error": output, **spec["payload"](config)}, status=500)

        state.log_hub.publish("Drive tuning saved. gamepad-teleop and robot-motion restarted.")
        return web.json_response({"ok": True, **spec["payload"](config)})

    if name == "sensors":
        state.log_hub.publish("Saving sensors config and restarting robot-sensors and robot-motion...")
        try:
            await asyncio.to_thread(spec["save"], config, path)
            sensors_result = await asyncio.to_thread(restart_robot_sensors)
            motion_result = await asyncio.to_thread(restart_robot_motion)
        except Exception as exc:
            state.log_hub.publish(f"Sensors config apply failed: {exc}")
            return web.json_response({"error": str(exc)}, status=500)

        if sensors_result.returncode != 0:
            output = (sensors_result.stderr or sensors_result.stdout).strip()
            state.log_hub.publish(f"Sensors config saved, but robot-sensors restart failed: {output}")
            return web.json_response({"error": output, **spec["payload"](config)}, status=500)
        if motion_result.returncode != 0:
            output = (motion_result.stderr or motion_result.stdout).strip()
            state.log_hub.publish(f"Sensors config saved, but robot-motion restart failed: {output}")
            return web.json_response({"error": output, **spec["payload"](config)}, status=500)

        state.log_hub.publish("Sensors config saved. robot-sensors and robot-motion restarted.")
        return web.json_response({"ok": True, **spec["payload"](config)})

    try:
        await asyncio.to_thread(spec["save"], config, path)
    except OSError as exc:
        state.log_hub.publish(f"{name} config save failed: {exc}")
        return web.json_response({"error": str(exc)}, status=500)

    state.log_hub.publish(spec["saved_log"])
    return web.json_response({"ok": True, **spec["payload"](config)})


async def voice_command_handler(request: web.Request) -> web.Response:
    state: WebDashboardState = request.app["state"]
    try:
        payload = await request.json()
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": f"Invalid JSON: {exc}"}, status=400)

    cmd = payload.get("cmd") if isinstance(payload, dict) else None
    if cmd not in {"talk_now", "end_session", "set_personality"}:
        return web.json_response({"error": f"unknown cmd: {cmd!r}"}, status=400)

    message: dict[str, Any] = {"cmd": cmd}
    if cmd == "set_personality":
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            return web.json_response({"error": "set_personality requires a name"}, status=400)
        message["name"] = name

    ack = await asyncio.to_thread(send_voice_command, state.voice_command_socket, message)
    if ack is None:
        return web.json_response({"error": "voice service not reachable"}, status=503)
    if not ack.get("ok", False):
        return web.json_response({"error": "voice command failed", "reason": ack.get("reason")}, status=502)
    if not ack.get("accepted", False):
        reason = ack.get("reason") or "ignored"
        return web.json_response({"error": reason, "reason": reason}, status=409)
    return web.json_response({"ok": True, "accepted": True})


async def voice_personalities_handler(request: web.Request) -> web.Response:
    return web.json_response({"personalities": sorted(load_personalities().keys())})


MODEL_FRAME_NAME_RE = re.compile(r"^[0-9]+-[a-z0-9_-]+\.jpg$")


async def model_frames_list_handler(request: web.Request) -> web.Response:
    base_dir = model_frames.MODEL_FRAMES_DIR
    if not base_dir.is_dir():
        return web.json_response({"frames": []})
    frames = []
    for path in sorted(base_dir.glob("*.jpg"), key=lambda item: item.name, reverse=True)[: model_frames.MAX_FRAMES]:
        caption_path = path.with_suffix(".txt")
        caption = ""
        if caption_path.is_file():
            caption = caption_path.read_text(encoding="utf-8")
        frames.append({"name": path.name, "t": path.stat().st_mtime, "caption": caption})
    return web.json_response({"frames": frames})


async def model_frame_file_handler(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    if MODEL_FRAME_NAME_RE.fullmatch(name) is None:
        raise web.HTTPNotFound()
    path = model_frames.MODEL_FRAMES_DIR / name
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Content-Type": "image/jpeg"})


def build_app(state: WebDashboardState) -> web.Application:
    app = web.Application(middlewares=[no_cache_middleware])
    app["state"] = state
    app.router.add_get("/", index_handler)
    app.router.add_get("/events", events_handler)
    app.router.add_get("/logs/events", logs_handler)
    app.router.add_get("/redeploy/status", redeploy_status_handler)
    app.router.add_post("/redeploy/arm", redeploy_arm_handler)
    app.router.add_post("/redeploy/run", redeploy_run_handler)
    app.router.add_get("/config/{name}", config_get)
    app.router.add_post("/config/{name}", config_apply)
    app.router.add_post("/voice/command", voice_command_handler)
    app.router.add_get("/voice/personalities", voice_personalities_handler)
    app.router.add_get("/api/model-frames", model_frames_list_handler)
    app.router.add_get("/model-frames/{name}", model_frame_file_handler)
    app.router.add_static("/static", str(state.static_dir), show_index=False)

    async def stop_journal_follower(_app: web.Application) -> None:
        await state.journal_follower.stop()

    app.on_cleanup.append(stop_journal_follower)
    return app


async def run_service(args: argparse.Namespace) -> None:
    loop = asyncio.get_running_loop()
    snapshot_store = SnapshotStore(loop)
    state = WebDashboardState(
        loop,
        snapshot_store,
        Path(args.static_dir),
        args.drive_tuning_config,
        args.vision_config,
        args.voice_config,
        args.voice_command_socket,
        args.sensors_config,
    )

    subscriber = TelemetrySubscriberThread(snapshot_store, args.telemetry_socket)
    subscriber.start()

    app = build_app(state)
    runner = web.AppRunner(app, shutdown_timeout=SHUTDOWN_TIMEOUT_SECONDS)
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
    parser.add_argument("--drive-tuning-config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--vision-config", default=DEFAULT_VISION_CONFIG_PATH)
    parser.add_argument("--voice-config", default=DEFAULT_VOICE_CONFIG_PATH)
    parser.add_argument("--sensors-config", default=DEFAULT_SENSORS_CONFIG_PATH)
    parser.add_argument("--voice-command-socket", default=DEFAULT_VOICE_COMMAND_SOCKET)
    parser.add_argument("--static-dir", default=str(STATIC_DIR))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run_service(args))


if __name__ == "__main__":
    main()
