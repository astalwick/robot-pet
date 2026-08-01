"""Voice-driven motion intents (express, move) for Personality Phase 2.

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
import math
import os
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from control.commands import MotionCommand


# Wheel-only expressions. We have no head or servos, so every emote is something
# differential-drive wheels can do: a left-right sway, a full spin, a quick "no" shake.
WIGGLE_HALF_DURATION = 0.25
WIGGLE_ANGULAR_Z = 0.5
SPIN_DURATION = 1.5
SPIN_ANGULAR_Z = 0.6
SHAKE_HALF_DURATION = 0.12
SHAKE_HALF_SWINGS = 6
SHAKE_ANGULAR_Z = 0.6
EXPRESS_KINDS = ("wiggle", "spin", "shake")

# `move` drives straight at a steady pace. distance_meters is signed: positive
# drives forward, negative drives backward. robot-motion stops the move using
# RoboClaw encoder travel, so the executor just keeps commanding motion while the
# intent is active. A full 1.0 here means a move runs at the same normal-mode speed
# as a full forward stick on the gamepad (the drive mixer's speed_scale governs the
# actual qpps), so voice/agent moves feel as brisk as teleop.
MOVE_LINEAR_X = 1.0
MOVE_MAX_DISTANCE_METERS = 2.0
DIAGNOSTIC_TURN_ANGULAR_Z = 0.3
DIAGNOSTIC_TURN_MIN_DURATION = 0.1
DIAGNOSTIC_TURN_MAX_DURATION = 4.0

# face_me reuses the verified turn command and direction signs from
# diagnostic_turn. robot-voice supplies a signed angle; the turn closes the loop
# on measured IMU yaw and stops once the robot has actually rotated that far,
# instead of estimating from elapsed time. Positive degrees turn toward the left
# drive wheel.
FACE_ME_ANGULAR_Z = DIAGNOSTIC_TURN_ANGULAR_Z
FACE_ME_ALREADY_FACING_DEGREES = 15
FACE_ME_MAX_RELATIVE_DEGREES = 180

# `turn` reuses the verified turn speed and direction signs from diagnostic_turn /
# face_me, and likewise stops on measured IMU yaw. Positive degrees turn the robot
# to its left.
TURN_ANGULAR_Z = DIAGNOSTIC_TURN_ANGULAR_Z
TURN_APPROACH_DEGREES = 10.0
TURN_APPROACH_ANGULAR_Z = TURN_ANGULAR_Z / 2
TURN_CORRECTION_TOLERANCE_DEGREES = 2.0
TURN_MAX_CORRECTIONS = 2
TURN_CORRECTION_MIN_SECONDS = 0.05
TURN_CORRECTION_MAX_SECONDS = 0.4
TURN_MIN_DEGREES = 1
TURN_MAX_DEGREES = 360

# Watchdog for yaw-closed-loop turns: if measured rotation does not advance by at
# least TURN_PROGRESS_DEGREES within TURN_NO_PROGRESS_TIMEOUT_SECONDS, give up
# instead of commanding the wheels forever. Mirrors the encoder-move no-progress
# stall in robot_motion. A healthy turn rotates ~36 deg/sec, so a stuck robot is
# obvious well inside the window.
TURN_PROGRESS_DEGREES = 1.0
TURN_NO_PROGRESS_TIMEOUT_SECONDS = 1.0

KNOWN_TOOLS = ("express", "move", "diagnostic_turn", "face_me", "turn")
DIAGNOSTIC_TURN_DIRECTIONS = ("toward_left_wheel", "toward_right_wheel")
# The bridge gives a motion intent this long to finish. An encoder move is bounded
# by distance and the no-progress watchdog, so this is just a generous upper bound.
MOTION_INTENT_REPLY_TIMEOUT_SECONDS = 35.0
# Clients wait a little longer so the bridge's own internal_timeout reply wins the
# race instead of both sides timing out at the same instant.
MOTION_INTENT_CLIENT_TIMEOUT_SECONDS = MOTION_INTENT_REPLY_TIMEOUT_SECONDS + 5.0


def valid_move_distance(value: Any) -> bool:
    """A finite, non-zero real number (not a bool). Magnitude is clamped later."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value != 0
    )


def valid_relative_degrees(value: Any) -> bool:
    """A real number (not a bool) within the signed turn range."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and -FACE_ME_MAX_RELATIVE_DEGREES <= value <= FACE_ME_MAX_RELATIVE_DEGREES
    )


def _yaw_delta(current: float, previous: float) -> float:
    """Signed degrees from previous to current yaw, unwrapped across the ±180 seam."""
    delta = (current - previous) % 360.0
    if delta > 180.0:
        delta -= 360.0
    return delta


@dataclass(frozen=True)
class IntentTick:
    """One control-loop tick of an active intent."""

    command: MotionCommand | None
    finished: bool
    result: str | None  # "completed" or "preempted_by_gamepad" when finished
    snap_stop: bool = False
    details: dict | None = None


@dataclass
class _ActiveIntent:
    tool: str
    started_at: float
    direction: str | None = None
    duration_seconds: float | None = None
    distance_meters: float | None = None
    relative_degrees: float | None = None
    degrees: float | None = None
    kind: str | None = None
    # Yaw-closed-loop turn state: the last yaw we saw and the signed degrees
    # rotated since the turn began, plus the no-progress watchdog's high-water
    # mark and the time we last saw real progress.
    last_yaw: float | None = None
    turned_degrees: float = 0.0
    turn_best_degrees: float = 0.0
    turn_progress_at: float | None = None
    turn_phase: str = "turning"
    turn_corrections: int = 0
    turn_correction_back: bool = False
    turn_stop_at: float | None = None
    turn_best_error: float | None = None
    turn_yaw_rate_degrees_per_second: float | None = None
    turn_command_angular_z: float | None = None
    last_yaw_sample_time: float | None = None


class MotionIntentExecutor:
    """Pure state machine. No threads, no IO."""

    def __init__(self) -> None:
        self._active: _ActiveIntent | None = None

    def is_active(self) -> bool:
        return self._active is not None

    def cancel(self) -> None:
        self._active = None

    def reset_active_start(self, now: float) -> None:
        if self._active is not None:
            self._active.started_at = now
            self._active.last_yaw = None
            self._active.turned_degrees = 0.0
            self._active.turn_best_degrees = 0.0
            self._active.turn_progress_at = None
            self._active.turn_phase = "turning"
            self._active.turn_corrections = 0
            self._active.turn_correction_back = False
            self._active.turn_stop_at = None
            self._active.turn_best_error = None
            self._active.turn_yaw_rate_degrees_per_second = None
            self._active.turn_command_angular_z = None
            self._active.last_yaw_sample_time = None

    def active_tool(self) -> str | None:
        return self._active.tool if self._active else None

    def start(
        self,
        tool: str,
        now: float,
        direction: str | None = None,
        duration_seconds: float | None = None,
        distance_meters: float | None = None,
        relative_degrees: float | None = None,
        degrees: float | None = None,
        kind: str | None = None,
    ) -> str | None:
        """Begin a new intent. Returns None on success, error string on failure."""
        if tool not in KNOWN_TOOLS:
            return "unknown_tool"
        if self._active is not None:
            return "busy"
        if tool == "express" and kind not in EXPRESS_KINDS:
            return "invalid_kind"
        if tool == "diagnostic_turn":
            if direction not in DIAGNOSTIC_TURN_DIRECTIONS:
                return "invalid_direction"
            if (
                not isinstance(duration_seconds, (int, float))
                or isinstance(duration_seconds, bool)
                or not DIAGNOSTIC_TURN_MIN_DURATION <= duration_seconds <= DIAGNOSTIC_TURN_MAX_DURATION
            ):
                return "invalid_duration"
        if tool == "move":
            if not valid_move_distance(distance_meters):
                return "invalid_distance"
            # Clamp an over-long distance to the max instead of rejecting it, keeping
            # the requested direction. robot-motion stops the move on encoder travel.
            magnitude = min(abs(distance_meters), MOVE_MAX_DISTANCE_METERS)
            distance_meters = magnitude if distance_meters > 0 else -magnitude
        if tool == "face_me" and not valid_relative_degrees(relative_degrees):
            return "invalid_relative_degrees"
        if tool == "turn":
            if (
                not isinstance(degrees, (int, float))
                or isinstance(degrees, bool)
                or not math.isfinite(degrees)
            ):
                return "invalid_degrees"
            magnitude = min(max(abs(degrees), TURN_MIN_DEGREES), TURN_MAX_DEGREES)
            degrees = magnitude if degrees >= 0 else -magnitude
        self._active = _ActiveIntent(
            tool=tool,
            started_at=now,
            direction=direction,
            duration_seconds=duration_seconds,
            distance_meters=distance_meters,
            relative_degrees=relative_degrees,
            degrees=degrees,
            kind=kind,
        )
        return None

    def active_move_distance_meters(self) -> float | None:
        """Signed distance of the active move intent, or None when no move is active."""
        if self._active is None or self._active.tool != "move":
            return None
        return self._active.distance_meters

    def tick(
        self,
        now: float,
        gamepad_active: bool,
        yaw_degrees: float | None = None,
        yaw_sample_time: float | None = None,
    ) -> IntentTick:
        if self._active is None:
            return IntentTick(command=None, finished=False, result=None)

        if gamepad_active:
            self._active = None
            return IntentTick(command=None, finished=True, result="preempted_by_gamepad")

        elapsed = now - self._active.started_at
        if self._active.tool == "express":
            command = self._express_command(elapsed)
            if command is not None:
                return IntentTick(command=command, finished=False, result=None)
            self._active = None
            return IntentTick(command=None, finished=True, result="completed")

        if self._active.tool == "move":
            # Encoder travel, not elapsed time, completes a move. robot-motion owns
            # the encoder reads and clears the intent; the executor just keeps the
            # wheels driving in the requested direction until then.
            linear_x = -MOVE_LINEAR_X if self._active.distance_meters < 0 else MOVE_LINEAR_X
            return IntentTick(
                command=MotionCommand(linear_x=linear_x, angular_z=0.0),
                finished=False,
                result=None,
            )

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

        if self._active.tool == "face_me":
            relative = self._active.relative_degrees
            if abs(relative) <= FACE_ME_ALREADY_FACING_DEGREES:
                return self._finish_turn("completed")
            return self._turn_tick(yaw_degrees, yaw_sample_time, abs(relative), now)

        if self._active.tool == "turn":
            degrees = self._active.degrees
            return self._turn_tick(yaw_degrees, yaw_sample_time, abs(degrees), now)

        self._active = None
        return IntentTick(command=None, finished=True, result="completed")

    def _turn_tick(
        self,
        yaw_degrees: float | None,
        yaw_sample_time: float | None,
        target_degrees: float,
        now: float,
    ) -> IntentTick:
        """Advance the active turn's yaw accumulator and say where it stands.

        Per-tick deltas are summed signed, so sensor noise cancels instead of
        creeping toward the target. The main turn runs at full speed until the
        last few degrees, then halves speed for the approach. Once the target is
        crossed, the wheels stop immediately and the executor waits for a fresh
        yaw sample before deciding whether a short correction is needed.
        """
        if yaw_degrees is None:
            return self._finish_turn("imu_unavailable")

        fresh_yaw = self._record_turn_yaw(yaw_degrees, yaw_sample_time, now)
        magnitude = abs(self._active.turned_degrees)
        error = target_degrees - magnitude

        if self._active.turn_phase == "checking":
            if not fresh_yaw:
                # Frozen yaw samples must not park the executor here forever:
                # the wheels are safely at zero, but a stuck intent blocks every
                # later one. The same no-progress window bounds the wait.
                if now - self._active.turn_progress_at >= TURN_NO_PROGRESS_TIMEOUT_SECONDS:
                    return self._finish_turn("turn_stalled")
                return IntentTick(
                    command=MotionCommand(linear_x=0.0, angular_z=0.0),
                    finished=False,
                    result=None,
                )
            if (
                abs(error) <= TURN_CORRECTION_TOLERANCE_DEGREES
                or self._active.turn_corrections >= TURN_MAX_CORRECTIONS
                or (
                    self._active.turn_corrections > 0
                    and self._active.turn_correction_back != (error < 0)
                )
            ):
                return self._finish_turn("completed")
            self._active.turn_phase = "correcting"
            self._active.turn_corrections += 1
            self._active.turn_correction_back = error < 0
            self._active.turn_best_error = abs(error)
            self._active.turn_progress_at = now

        angular_z = self._turn_angular_z(correcting_back=error < 0)
        if self._active.turn_phase == "correcting" or error <= TURN_APPROACH_DEGREES:
            angular_z = TURN_APPROACH_ANGULAR_Z if angular_z > 0 else -TURN_APPROACH_ANGULAR_Z
        self._set_turn_command(angular_z)

        timed_stop = self._active.turn_stop_at is not None and now >= self._active.turn_stop_at
        if self._active.turn_phase == "correcting":
            stop = (
                abs(error) <= TURN_CORRECTION_TOLERANCE_DEGREES
                or self._active.turn_correction_back != (error < 0)
                or timed_stop
            )
        else:
            stop = magnitude >= target_degrees or timed_stop
        if stop:
            self._active.turn_phase = "checking"
            self._active.turn_progress_at = now
            return IntentTick(
                command=MotionCommand(linear_x=0.0, angular_z=0.0),
                finished=False,
                result=None,
                snap_stop=True,
            )

        if self._turn_stalled(magnitude, abs(error), now):
            return self._finish_turn("turn_stalled")

        if fresh_yaw or self._active.turn_stop_at is None:
            self._active.turn_stop_at = self._turn_stop_at(
                abs(error),
                now,
                allow_partial_pulse=self._active.turn_phase == "correcting",
            )
        return IntentTick(
            command=MotionCommand(linear_x=0.0, angular_z=angular_z),
            finished=False,
            result=None,
        )

    def _finish_turn(self, result: str) -> IntentTick:
        turned = self._active.turned_degrees
        if self._active.tool == "face_me":
            commanded = self._active.relative_degrees
        else:
            commanded = self._active.degrees
        if commanded is not None and commanded != 0:
            measured = math.copysign(abs(turned), commanded)
        else:
            measured = turned
        self._active = None
        return IntentTick(
            command=None,
            finished=True,
            result=result,
            details={"measured_degrees": measured},
        )

    def _record_turn_yaw(
        self, yaw_degrees: float, yaw_sample_time: float | None, now: float
    ) -> bool:
        if yaw_sample_time is not None and yaw_sample_time == self._active.last_yaw_sample_time:
            return False
        sample_time = yaw_sample_time if yaw_sample_time is not None else now
        if self._active.last_yaw is None:
            self._active.last_yaw = yaw_degrees
            self._active.last_yaw_sample_time = sample_time
            self._active.turn_progress_at = now
            return True

        delta = _yaw_delta(yaw_degrees, self._active.last_yaw)
        previous_time = self._active.last_yaw_sample_time
        if previous_time is not None and sample_time > previous_time:
            self._active.turn_yaw_rate_degrees_per_second = abs(delta) / (sample_time - previous_time)
        self._active.turned_degrees += delta
        self._active.last_yaw = yaw_degrees
        self._active.last_yaw_sample_time = sample_time
        return True

    def _turn_stop_at(self, error: float, now: float, allow_partial_pulse: bool) -> float | None:
        rate = self._active.turn_yaw_rate_degrees_per_second
        if rate is None or rate <= 0.0:
            return None
        duration = error / rate
        if duration > TURN_CORRECTION_MAX_SECONDS and not allow_partial_pulse:
            return None
        duration = max(TURN_CORRECTION_MIN_SECONDS, min(TURN_CORRECTION_MAX_SECONDS, duration))
        return now + duration

    def _set_turn_command(self, angular_z: float) -> None:
        if self._active.turn_command_angular_z == angular_z:
            return
        same_speed = (
            self._active.turn_command_angular_z is not None
            and abs(self._active.turn_command_angular_z) == abs(angular_z)
        )
        self._active.turn_command_angular_z = angular_z
        # Direction flips can reuse the latest yaw rate; speed changes need a fresh sample.
        if not same_speed:
            self._active.turn_yaw_rate_degrees_per_second = None
        self._active.turn_stop_at = None

    def _turn_stalled(self, magnitude: float, error: float, now: float) -> bool:
        if self._active.turn_phase == "correcting":
            if self._active.turn_best_error is None or error < self._active.turn_best_error - TURN_PROGRESS_DEGREES:
                self._active.turn_best_error = error
                self._active.turn_progress_at = now
                return False
            return now - self._active.turn_progress_at >= TURN_NO_PROGRESS_TIMEOUT_SECONDS

        if magnitude > self._active.turn_best_degrees + TURN_PROGRESS_DEGREES:
            self._active.turn_best_degrees = magnitude
            self._active.turn_progress_at = now
            return False
        return now - self._active.turn_progress_at >= TURN_NO_PROGRESS_TIMEOUT_SECONDS

    def _turn_angular_z(self, correcting_back: bool = False) -> float:
        if self._active.tool == "face_me":
            angular_z = -FACE_ME_ANGULAR_Z if self._active.relative_degrees > 0 else FACE_ME_ANGULAR_Z
        else:
            angular_z = -TURN_ANGULAR_Z if self._active.degrees > 0 else TURN_ANGULAR_Z
        return -angular_z if correcting_back else angular_z

    def _express_command(self, elapsed: float) -> MotionCommand | None:
        """The wheel command for an in-progress express, or None when it has finished."""
        kind = self._active.kind
        if kind == "wiggle":
            if elapsed < WIGGLE_HALF_DURATION:
                return MotionCommand(linear_x=0.0, angular_z=WIGGLE_ANGULAR_Z)
            if elapsed < 2 * WIGGLE_HALF_DURATION:
                return MotionCommand(linear_x=0.0, angular_z=-WIGGLE_ANGULAR_Z)
            return None
        if kind == "spin":
            if elapsed < SPIN_DURATION:
                return MotionCommand(linear_x=0.0, angular_z=SPIN_ANGULAR_Z)
            return None
        if kind == "shake":
            if elapsed < SHAKE_HALF_SWINGS * SHAKE_HALF_DURATION:
                swing = int(elapsed / SHAKE_HALF_DURATION)
                angular_z = SHAKE_ANGULAR_Z if swing % 2 == 0 else -SHAKE_ANGULAR_Z
                return MotionCommand(linear_x=0.0, angular_z=angular_z)
            return None
        return None


class MotionIntentBridge:
    """Unix-socket server thread that hands intents to the main control loop.

    The main loop polls `take_pending()` each tick; if a request is waiting it
    runs through the executor and reports the outcome via the returned callback.
    The reply comes when the motion finishes, so a caller that issues a motion
    cannot start another action until this one is done.
    """

    INTENT_MAX_SECONDS = MOTION_INTENT_REPLY_TIMEOUT_SECONDS
    _ACCEPT_TIMEOUT = 0.5

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self._lock = threading.Lock()
        self._pending: dict[str, Any] | None = None
        self._stop_requested = False
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

    def take_stop(self) -> bool:
        """True once if a stop was requested since the last check. Clears the flag."""
        with self._lock:
            if not self._stop_requested:
                return False
            self._stop_requested = False
            return True

    def discard_pending(self) -> None:
        """Drop a queued request that has not started yet, replying that it was stopped.

        A stop cancels the active move, but a request can also be sitting in the single
        pending slot waiting to start. Without this, the loop would stop the robot and
        then immediately begin the queued move on the next tick.
        """
        pending = self.take_pending()
        if pending is not None:
            _, complete = pending
            complete({"ok": False, "error": "stopped"})

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            # Each connection runs on its own thread. A normal motion request blocks
            # inside _handle_connection until the move finishes, so handling it inline
            # here would stall accept() and a later stop could not be picked up until
            # the move was already done. Per-connection threads keep the stop fast-path
            # actually fast.
            threading.Thread(
                target=self._serve_connection, args=(conn,), name="motion-intent-conn", daemon=True
            ).start()

    def _serve_connection(self, conn: socket.socket) -> None:
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
        # Stop is a fast-path: it must take effect even while a motion is in flight,
        # so it never queues behind the single _pending slot. The main loop picks up
        # the flag on its next tick and cancels whatever is moving.
        if tool == "stop":
            with self._lock:
                self._stop_requested = True
            self._send(conn, {"ok": True, "result": "stopping"})
            return
        if tool not in KNOWN_TOOLS:
            self._send(conn, {"ok": False, "error": "unknown_tool"})
            return
        if tool == "express" and request.get("kind") not in EXPRESS_KINDS:
            self._send(conn, {"ok": False, "error": "invalid_kind"})
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
        if tool == "move":
            # Move is distance-only. Reject the legacy timed-move field so it can't
            # ride along silently and resurrect the old time-based contract.
            if request.get("duration_seconds") is not None:
                self._send(conn, {"ok": False, "error": "unexpected_duration"})
                return
            # Magnitude is clamped by the executor, so only validity is rejected here.
            # A move needs a finite, non-zero signed distance; NaN/inf/0/bool are out.
            if not valid_move_distance(request.get("distance_meters")):
                self._send(conn, {"ok": False, "error": "invalid_distance"})
                return
        if tool == "face_me" and not valid_relative_degrees(request.get("relative_degrees")):
            self._send(conn, {"ok": False, "error": "invalid_relative_degrees"})
            return
        if tool == "turn":
            degrees = request.get("degrees")
            if (
                not isinstance(degrees, (int, float))
                or isinstance(degrees, bool)
                or not math.isfinite(degrees)
            ):
                self._send(conn, {"ok": False, "error": "invalid_degrees"})
                return

        done_event = threading.Event()
        holder: list[dict[str, Any] | None] = [None]
        entry = {"request": request, "done_event": done_event, "holder": holder}
        with self._lock:
            if self._pending is not None:
                self._send(conn, {"ok": False, "error": "busy"})
                return
            self._pending = entry

        if not done_event.wait(timeout=self.INTENT_MAX_SECONDS):
            # Only withdraw our own request. The main loop may have taken it
            # already, and another connection's request may now hold the slot.
            with self._lock:
                if self._pending is entry:
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
    timeout: float = MOTION_INTENT_CLIENT_TIMEOUT_SECONDS,
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
