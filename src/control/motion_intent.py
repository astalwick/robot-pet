"""Voice-driven motion intents (wiggle, move_forward) for Personality Phase 2.

A motion intent is a small, time-bounded movement requested by a voice tool call.
The executor here is a pure state machine: it accepts a request, then on each
control-loop tick produces either a `MotionCommand` to drive the wheels or a
completion result. The gamepad always wins — if the gamepad becomes active
mid-intent, the intent is preempted and the executor returns to idle.

This module also contains the small Unix-socket client that voice uses to send
intents to whichever process is currently acting as the motion executor.
Personality Phase 2 ships with `gamepad-teleop` as that process; Body Phase 2
will replace it with a proper `robot-motion` service.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from control.commands import MotionCommand


WIGGLE_HALF_DURATION = 0.25
WIGGLE_ANGULAR_Z = 0.5
MOVE_FORWARD_DURATION = 0.5
MOVE_FORWARD_LINEAR_X = 0.3
DIAGNOSTIC_TURN_ANGULAR_Z = 0.3
DIAGNOSTIC_TURN_MIN_DURATION = 0.1
DIAGNOSTIC_TURN_MAX_DURATION = 2.0

KNOWN_TOOLS = ("wiggle", "move_forward", "diagnostic_turn")
DIAGNOSTIC_TURN_DIRECTIONS = ("toward_left_wheel", "toward_right_wheel")


@dataclass(frozen=True)
class IntentTick:
    """One control-loop tick of an active intent."""

    command: MotionCommand | None
    finished: bool
    result: str | None  # "completed" or "preempted_by_gamepad" when finished


@dataclass
class _ActiveIntent:
    tool: str
    started_at: float
    direction: str | None = None
    duration_seconds: float | None = None


class MotionIntentExecutor:
    """Pure state machine. No threads, no IO."""

    def __init__(self) -> None:
        self._active: _ActiveIntent | None = None

    def is_active(self) -> bool:
        return self._active is not None

    def active_tool(self) -> str | None:
        return self._active.tool if self._active else None

    def start(
        self,
        tool: str,
        now: float,
        direction: str | None = None,
        duration_seconds: float | None = None,
    ) -> str | None:
        """Begin a new intent. Returns None on success, error string on failure."""
        if tool not in KNOWN_TOOLS:
            return "unknown_tool"
        if self._active is not None:
            return "busy"
        if tool == "diagnostic_turn":
            if direction not in DIAGNOSTIC_TURN_DIRECTIONS:
                return "invalid_direction"
            if (
                not isinstance(duration_seconds, (int, float))
                or isinstance(duration_seconds, bool)
                or not DIAGNOSTIC_TURN_MIN_DURATION <= duration_seconds <= DIAGNOSTIC_TURN_MAX_DURATION
            ):
                return "invalid_duration"
        self._active = _ActiveIntent(
            tool=tool,
            started_at=now,
            direction=direction,
            duration_seconds=duration_seconds,
        )
        return None

    def tick(self, now: float, gamepad_active: bool) -> IntentTick:
        if self._active is None:
            return IntentTick(command=None, finished=False, result=None)

        if gamepad_active:
            self._active = None
            return IntentTick(command=None, finished=True, result="preempted_by_gamepad")

        elapsed = now - self._active.started_at
        if self._active.tool == "wiggle":
            if elapsed < WIGGLE_HALF_DURATION:
                return IntentTick(
                    command=MotionCommand(linear_x=0.0, angular_z=WIGGLE_ANGULAR_Z),
                    finished=False,
                    result=None,
                )
            if elapsed < 2 * WIGGLE_HALF_DURATION:
                return IntentTick(
                    command=MotionCommand(linear_x=0.0, angular_z=-WIGGLE_ANGULAR_Z),
                    finished=False,
                    result=None,
                )
            self._active = None
            return IntentTick(command=None, finished=True, result="completed")

        if self._active.tool == "move_forward":
            if elapsed < MOVE_FORWARD_DURATION:
                return IntentTick(
                    command=MotionCommand(linear_x=MOVE_FORWARD_LINEAR_X, angular_z=0.0),
                    finished=False,
                    result=None,
                )
            self._active = None
            return IntentTick(command=None, finished=True, result="completed")

        if self._active.tool == "diagnostic_turn":
            if elapsed < self._active.duration_seconds:
                angular_z = (
                    -DIAGNOSTIC_TURN_ANGULAR_Z
                    if self._active.direction == "toward_left_wheel"
                    else DIAGNOSTIC_TURN_ANGULAR_Z
                )
                return IntentTick(
                    command=MotionCommand(linear_x=0.0, angular_z=angular_z),
                    finished=False,
                    result=None,
                )
            self._active = None
            return IntentTick(command=None, finished=True, result="completed")

        self._active = None
        return IntentTick(command=None, finished=True, result="completed")


class MotionIntentBridge:
    """Unix-socket server thread that hands intents to the main control loop.

    The main loop polls `take_pending()` each tick; if a request is waiting it
    runs through the executor and reports the outcome via the returned
    completion callback. The server thread blocks the client connection until
    completion is signaled, so the LLM sees a final result.
    """

    INTENT_MAX_SECONDS = 5.0
    _ACCEPT_TIMEOUT = 0.5

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self._lock = threading.Lock()
        self._pending: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._server: socket.socket | None = None

    def start(self) -> None:
        parent = os.path.dirname(self.socket_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o660)
        server.listen(4)
        server.settimeout(self._ACCEPT_TIMEOUT)
        self._server = server
        self._thread = threading.Thread(target=self._serve, name="motion-intent", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def take_pending(self) -> tuple[dict[str, Any], Callable[[dict[str, Any]], None]] | None:
        with self._lock:
            if self._pending is None:
                return None
            entry = self._pending
            self._pending = None

        done_event: threading.Event = entry["done_event"]
        holder: list[dict[str, Any] | None] = entry["holder"]

        def complete(response: dict[str, Any]) -> None:
            holder[0] = response
            done_event.set()

        return entry["request"], complete

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            try:
                self._handle_connection(conn)
            finally:
                conn.close()

    def _handle_connection(self, conn: socket.socket) -> None:
        conn.settimeout(2.0)
        buffer = b""
        try:
            while b"\n" not in buffer:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buffer += chunk
                if len(buffer) > 4096:
                    self._send(conn, {"ok": False, "error": "bad_request"})
                    return
        except (TimeoutError, socket.timeout, OSError):
            return

        line = buffer.partition(b"\n")[0]
        try:
            request = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(conn, {"ok": False, "error": "bad_request"})
            return
        if not isinstance(request, dict):
            self._send(conn, {"ok": False, "error": "bad_request"})
            return

        tool = request.get("tool", "")
        if tool not in KNOWN_TOOLS:
            self._send(conn, {"ok": False, "error": "unknown_tool"})
            return
        if tool == "diagnostic_turn":
            direction = request.get("direction")
            duration_seconds = request.get("duration_seconds")
            if direction not in DIAGNOSTIC_TURN_DIRECTIONS:
                self._send(conn, {"ok": False, "error": "invalid_direction"})
                return
            if (
                not isinstance(duration_seconds, (int, float))
                or isinstance(duration_seconds, bool)
                or not DIAGNOSTIC_TURN_MIN_DURATION <= duration_seconds <= DIAGNOSTIC_TURN_MAX_DURATION
            ):
                self._send(conn, {"ok": False, "error": "invalid_duration"})
                return

        done_event = threading.Event()
        holder: list[dict[str, Any] | None] = [None]
        with self._lock:
            if self._pending is not None:
                self._send(conn, {"ok": False, "error": "busy"})
                return
            self._pending = {"request": request, "done_event": done_event, "holder": holder}

        if not done_event.wait(timeout=self.INTENT_MAX_SECONDS):
            with self._lock:
                self._pending = None
            self._send(conn, {"ok": False, "error": "internal_timeout"})
            return

        response = holder[0] or {"ok": False, "error": "no_response"}
        self._send(conn, response)

    def _send(self, conn: socket.socket, response: dict[str, Any]) -> None:
        payload = (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            conn.sendall(payload)
        except OSError:
            pass


def request_motion_intent(
    socket_path: str,
    tool: str,
    timeout: float = 2.0,
    **parameters: Any,
) -> dict[str, Any]:
    """Send a motion intent request over a Unix socket and wait for one reply.

    Returns a dict with at least an "ok" boolean. On transport failure, returns
    `{"ok": False, "error": "..."}` instead of raising — the caller (voice tool
    handler) speaks the error back through the LLM, so it must always get a
    response it can serialize.
    """
    request = json.dumps({"tool": tool, **parameters}, separators=(",", ":")) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(socket_path)
            client.sendall(request.encode("utf-8"))
            buffer = b""
            while b"\n" not in buffer:
                chunk = client.recv(4096)
                if not chunk:
                    break
                buffer += chunk
    except FileNotFoundError:
        return {"ok": False, "error": "motion_socket_missing"}
    except (ConnectionRefusedError, ConnectionResetError, BrokenPipeError):
        return {"ok": False, "error": "motion_socket_refused"}
    except TimeoutError:
        return {"ok": False, "error": "timeout"}
    except OSError as exc:
        return {"ok": False, "error": f"socket_error: {exc}"}

    line, _, _ = buffer.partition(b"\n")
    if not line:
        return {"ok": False, "error": "no_response"}
    try:
        return json.loads(line.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "bad_response"}
