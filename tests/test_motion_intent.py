import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from control.commands import MotionCommand
from control.motion_intent import (
    DIAGNOSTIC_TURN_ANGULAR_Z,
    DIAGNOSTIC_TURN_MAX_DURATION,
    DIAGNOSTIC_TURN_MIN_DURATION,
    FACE_ME_ANGULAR_Z,
    FACE_ME_DEGREES_PER_SECOND,
    MOVE_LINEAR_X,
    MOVE_MAX_DISTANCE_METERS,
    TURN_ANGULAR_Z,
    TURN_DEGREES_PER_SECOND,
    TURN_MAX_DEGREES,
    TURN_MIN_DEGREES,
    WIGGLE_ANGULAR_Z,
    WIGGLE_HALF_DURATION,
    MotionIntentBridge,
    MotionIntentExecutor,
    request_motion_intent,
)
from voice.assistant import VoiceState, stream_openai_words


class MotionIntentExecutorTest(unittest.TestCase):
    def test_unknown_tool_rejected(self):
        executor = MotionIntentExecutor()
        self.assertEqual(executor.start("spin", now=0.0), "unknown_tool")
        self.assertFalse(executor.is_active())

    def test_second_request_while_busy_rejected(self):
        executor = MotionIntentExecutor()
        self.assertIsNone(executor.start("express", now=0.0, kind="wiggle"))
        self.assertEqual(executor.start("express", now=0.05, kind="wiggle"), "busy")

    def test_cancel_clears_active_intent(self):
        executor = MotionIntentExecutor()
        executor.start("express", now=0.0, kind="wiggle")
        self.assertTrue(executor.is_active())

        executor.cancel()

        self.assertFalse(executor.is_active())

    def test_reset_active_start_restarts_elapsed_time(self):
        executor = MotionIntentExecutor()
        executor.start("express", now=0.0, kind="wiggle")

        executor.reset_active_start(now=2.0)
        tick = executor.tick(now=2.1, gamepad_active=False)

        self.assertEqual(tick.command, MotionCommand(0.0, WIGGLE_ANGULAR_Z))
        self.assertFalse(tick.finished)

    def test_express_rejects_invalid_kind(self):
        executor = MotionIntentExecutor()
        self.assertEqual(executor.start("express", now=0.0, kind="nod"), "invalid_kind")
        self.assertEqual(executor.start("express", now=0.0), "invalid_kind")

    def test_express_wiggle_emits_left_then_right_then_completes(self):
        executor = MotionIntentExecutor()
        executor.start("express", now=0.0, kind="wiggle")

        first = executor.tick(now=0.1, gamepad_active=False)
        self.assertEqual(first.command, MotionCommand(0.0, WIGGLE_ANGULAR_Z))
        self.assertFalse(first.finished)

        second = executor.tick(now=WIGGLE_HALF_DURATION + 0.05, gamepad_active=False)
        self.assertEqual(second.command, MotionCommand(0.0, -WIGGLE_ANGULAR_Z))
        self.assertFalse(second.finished)

        done = executor.tick(now=2 * WIGGLE_HALF_DURATION + 0.05, gamepad_active=False)
        self.assertTrue(done.finished)
        self.assertEqual(done.result, "completed")
        self.assertFalse(executor.is_active())

    def test_move_drives_forward_for_positive_distance(self):
        # robot-motion stops the move on encoder travel, so the executor keeps
        # commanding forward across ticks and never finishes from elapsed time.
        executor = MotionIntentExecutor()
        self.assertIsNone(executor.start("move", now=0.0, distance_meters=1.0))

        first = executor.tick(now=0.1, gamepad_active=False)
        self.assertEqual(first.command, MotionCommand(MOVE_LINEAR_X, 0.0))
        self.assertFalse(first.finished)

        later = executor.tick(now=999.0, gamepad_active=False)
        self.assertEqual(later.command, MotionCommand(MOVE_LINEAR_X, 0.0))
        self.assertFalse(later.finished)
        self.assertEqual(executor.active_move_distance_meters(), 1.0)

    def test_move_drives_backward_for_negative_distance(self):
        executor = MotionIntentExecutor()
        self.assertIsNone(executor.start("move", now=0.0, distance_meters=-1.0))

        running = executor.tick(now=0.1, gamepad_active=False)
        self.assertEqual(running.command, MotionCommand(-MOVE_LINEAR_X, 0.0))
        self.assertFalse(running.finished)
        self.assertEqual(executor.active_move_distance_meters(), -1.0)

    def test_move_clamps_out_of_range_distance(self):
        # An over-long forward distance clamps down to the max, staying forward.
        executor = MotionIntentExecutor()
        self.assertIsNone(executor.start("move", now=0.0, distance_meters=99.0))
        self.assertEqual(executor.active_move_distance_meters(), MOVE_MAX_DISTANCE_METERS)

        # An over-long reverse distance clamps to the negated max.
        executor = MotionIntentExecutor()
        self.assertIsNone(executor.start("move", now=0.0, distance_meters=-99.0))
        self.assertEqual(executor.active_move_distance_meters(), -MOVE_MAX_DISTANCE_METERS)

    def test_move_rejects_invalid_distance(self):
        executor = MotionIntentExecutor()
        self.assertEqual(executor.start("move", now=0.0), "invalid_distance")
        self.assertEqual(executor.start("move", now=0.0, distance_meters=True), "invalid_distance")
        self.assertEqual(executor.start("move", now=0.0, distance_meters="far"), "invalid_distance")
        self.assertEqual(executor.start("move", now=0.0, distance_meters=0), "invalid_distance")
        self.assertEqual(executor.start("move", now=0.0, distance_meters=float("nan")), "invalid_distance")
        self.assertEqual(executor.start("move", now=0.0, distance_meters=float("inf")), "invalid_distance")

    def test_gamepad_active_preempts_mid_intent(self):
        executor = MotionIntentExecutor()
        executor.start("move", now=0.0, distance_meters=1.0)

        tick = executor.tick(now=0.1, gamepad_active=True)
        self.assertTrue(tick.finished)
        self.assertEqual(tick.result, "preempted_by_gamepad")
        self.assertIsNone(tick.command)
        self.assertFalse(executor.is_active())

    def test_diagnostic_turn_toward_left_wheel_runs_for_requested_duration(self):
        executor = MotionIntentExecutor()
        self.assertIsNone(
            executor.start(
                "diagnostic_turn",
                now=0.0,
                direction="toward_left_wheel",
                duration_seconds=0.5,
            )
        )

        running = executor.tick(now=0.4, gamepad_active=False)
        self.assertEqual(running.command, MotionCommand(0.0, -DIAGNOSTIC_TURN_ANGULAR_Z))

        done = executor.tick(now=0.5, gamepad_active=False)
        self.assertTrue(done.finished)
        self.assertEqual(done.result, "completed")

    def test_diagnostic_turn_toward_right_wheel_uses_opposite_direction(self):
        executor = MotionIntentExecutor()
        executor.start(
            "diagnostic_turn",
            now=0.0,
            direction="toward_right_wheel",
            duration_seconds=0.5,
        )

        running = executor.tick(now=0.1, gamepad_active=False)

        self.assertEqual(running.command, MotionCommand(0.0, DIAGNOSTIC_TURN_ANGULAR_Z))

    def test_diagnostic_turn_rejects_invalid_parameters(self):
        executor = MotionIntentExecutor()

        self.assertEqual(
            executor.start("diagnostic_turn", now=0.0, direction="left", duration_seconds=0.5),
            "invalid_direction",
        )
        self.assertEqual(
            executor.start(
                "diagnostic_turn",
                now=0.0,
                direction="toward_left_wheel",
                duration_seconds=DIAGNOSTIC_TURN_MIN_DURATION - 0.01,
            ),
            "invalid_duration",
        )
        self.assertEqual(
            executor.start(
                "diagnostic_turn",
                now=0.0,
                direction="toward_left_wheel",
                duration_seconds=DIAGNOSTIC_TURN_MAX_DURATION + 0.01,
            ),
            "invalid_duration",
        )
    def test_face_me_positive_turns_toward_left_wheel(self):
        executor = MotionIntentExecutor()
        self.assertIsNone(executor.start("face_me", now=0.0, relative_degrees=90))

        running = executor.tick(now=0.5, gamepad_active=False)
        self.assertEqual(running.command, MotionCommand(0.0, -FACE_ME_ANGULAR_Z))
        self.assertFalse(running.finished)

        # Done after one full turn at the configured degrees-per-second rate.
        done = executor.tick(now=90 / FACE_ME_DEGREES_PER_SECOND + 0.05, gamepad_active=False)
        self.assertTrue(done.finished)
        self.assertEqual(done.result, "completed")

    def test_face_me_negative_turns_toward_right_wheel(self):
        executor = MotionIntentExecutor()
        executor.start("face_me", now=0.0, relative_degrees=-90)

        running = executor.tick(now=0.5, gamepad_active=False)
        self.assertEqual(running.command, MotionCommand(0.0, FACE_ME_ANGULAR_Z))

    def test_face_me_within_fifteen_degrees_completes_without_moving(self):
        executor = MotionIntentExecutor()
        executor.start("face_me", now=0.0, relative_degrees=15)

        tick = executor.tick(now=0.0, gamepad_active=False)
        self.assertTrue(tick.finished)
        self.assertEqual(tick.result, "completed")
        self.assertIsNone(tick.command)

    def test_turn_positive_degrees_turns_left_then_completes(self):
        executor = MotionIntentExecutor()
        self.assertIsNone(executor.start("turn", now=0.0, degrees=90))

        # Still turning just before the configured duration, done just after.
        running = executor.tick(now=1.0, gamepad_active=False)
        self.assertEqual(running.command, MotionCommand(0.0, -TURN_ANGULAR_Z))
        self.assertFalse(running.finished)

        done = executor.tick(now=90 / TURN_DEGREES_PER_SECOND + 0.05, gamepad_active=False)
        self.assertTrue(done.finished)
        self.assertEqual(done.result, "completed")

    def test_turn_negative_degrees_turns_right(self):
        executor = MotionIntentExecutor()
        executor.start("turn", now=0.0, degrees=-45)

        running = executor.tick(now=0.1, gamepad_active=False)
        self.assertEqual(running.command, MotionCommand(0.0, TURN_ANGULAR_Z))

    def test_turn_rejects_non_numeric_degrees(self):
        executor = MotionIntentExecutor()
        self.assertEqual(executor.start("turn", now=0.0, degrees=True), "invalid_degrees")
        self.assertEqual(executor.start("turn", now=0.0, degrees=None), "invalid_degrees")
        self.assertEqual(executor.start("turn", now=0.0, degrees=float("nan")), "invalid_degrees")
        self.assertEqual(executor.start("turn", now=0.0, degrees=float("inf")), "invalid_degrees")

    def test_turn_clamps_out_of_range_degrees(self):
        # Zero degrees clamps up to the minimum so a turn still happens.
        executor = MotionIntentExecutor()
        self.assertIsNone(executor.start("turn", now=0.0, degrees=0))
        duration = TURN_MIN_DEGREES / TURN_DEGREES_PER_SECOND
        self.assertTrue(executor.tick(now=duration + 0.05, gamepad_active=False).finished)

        # An over-large magnitude clamps down to a single full turn.
        executor = MotionIntentExecutor()
        self.assertIsNone(executor.start("turn", now=0.0, degrees=TURN_MAX_DEGREES + 1))
        duration = TURN_MAX_DEGREES / TURN_DEGREES_PER_SECOND
        self.assertFalse(executor.tick(now=duration - 0.05, gamepad_active=False).finished)
        self.assertTrue(executor.tick(now=duration + 0.05, gamepad_active=False).finished)

    def test_face_me_gamepad_activity_preempts(self):
        executor = MotionIntentExecutor()
        executor.start("face_me", now=0.0, relative_degrees=90)

        tick = executor.tick(now=0.2, gamepad_active=True)
        self.assertTrue(tick.finished)
        self.assertEqual(tick.result, "preempted_by_gamepad")
        self.assertFalse(executor.is_active())

    def test_face_me_rejects_invalid_relative_degrees(self):
        executor = MotionIntentExecutor()
        self.assertEqual(executor.start("face_me", now=0.0), "invalid_relative_degrees")
        self.assertEqual(
            executor.start("face_me", now=0.0, relative_degrees="90"),
            "invalid_relative_degrees",
        )
        self.assertEqual(
            executor.start("face_me", now=0.0, relative_degrees=True),
            "invalid_relative_degrees",
        )
        self.assertEqual(
            executor.start("face_me", now=0.0, relative_degrees=181),
            "invalid_relative_degrees",
        )

    def test_face_me_accepts_range_endpoints(self):
        for value in (-180, 0, 180):
            executor = MotionIntentExecutor()
            self.assertIsNone(executor.start("face_me", now=0.0, relative_degrees=value))


class MotionIntentBridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmpdir.name, "motion.sock")
        self.bridge = MotionIntentBridge(self.socket_path)
        self.bridge.start()

    def tearDown(self):
        self.bridge.stop()
        self.tmpdir.cleanup()

    def _send_request_threaded(self, tool, **parameters):
        result_holder: list[dict] = []

        def worker():
            result_holder.append(request_motion_intent(self.socket_path, tool, timeout=2.0, **parameters))

        thread = threading.Thread(target=worker)
        thread.start()
        return thread, result_holder

    def test_request_arrives_at_main_loop_and_completion_reaches_client(self):
        thread, holder = self._send_request_threaded("express", kind="wiggle")

        deadline = time.monotonic() + 2.0
        pending = None
        while pending is None and time.monotonic() < deadline:
            pending = self.bridge.take_pending()
            if pending is None:
                time.sleep(0.01)
        self.assertIsNotNone(pending)

        request, complete = pending
        self.assertEqual(request, {"tool": "express", "kind": "wiggle"})
        complete({"ok": True, "result": "completed"})

        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(holder[0], {"ok": True, "result": "completed"})

    def test_unknown_tool_rejected_at_socket(self):
        result = request_motion_intent(self.socket_path, "spin", timeout=2.0)
        self.assertEqual(result, {"ok": False, "error": "unknown_tool"})

    def _take_pending(self):
        deadline = time.monotonic() + 2.0
        pending = None
        while pending is None and time.monotonic() < deadline:
            pending = self.bridge.take_pending()
            if pending is None:
                time.sleep(0.01)
        self.assertIsNotNone(pending)
        return pending

    def test_move_distance_arrives_at_main_loop(self):
        thread, holder = self._send_request_threaded("move", distance_meters=0.5)

        request, complete = self._take_pending()
        self.assertEqual(request, {"tool": "move", "distance_meters": 0.5})
        complete({"ok": True, "result": "completed"})

        thread.join(timeout=2.0)
        self.assertEqual(holder[0], {"ok": True, "result": "completed"})

    def test_move_over_range_distance_is_accepted_and_clamped(self):
        # The socket accepts an over-long distance; the executor clamps the magnitude
        # while keeping the requested direction.
        thread, holder = self._send_request_threaded("move", distance_meters=99.0)

        request, complete = self._take_pending()
        self.assertEqual(request["distance_meters"], 99.0)
        executor = MotionIntentExecutor()
        self.assertIsNone(
            executor.start("move", now=0.0, distance_meters=request["distance_meters"])
        )
        self.assertEqual(executor.active_move_distance_meters(), MOVE_MAX_DISTANCE_METERS)
        complete({"ok": True, "result": "completed"})

        thread.join(timeout=2.0)
        self.assertEqual(holder[0], {"ok": True, "result": "completed"})

    def test_move_rejects_duration_seconds_at_socket(self):
        # The legacy timed-move field is gone: a move carrying duration_seconds is
        # rejected, even alongside a valid distance.
        self.assertEqual(
            request_motion_intent(self.socket_path, "move", timeout=2.0, duration_seconds=1.0),
            {"ok": False, "error": "unexpected_duration"},
        )
        self.assertEqual(
            request_motion_intent(
                self.socket_path, "move", timeout=2.0, distance_meters=0.5, duration_seconds=1.0
            ),
            {"ok": False, "error": "unexpected_duration"},
        )

    def test_move_rejects_invalid_distance_at_socket(self):
        for parameters in (
            {},
            {"distance_meters": "far"},
            {"distance_meters": True},
            {"distance_meters": 0},
            {"distance_meters": float("nan")},
            {"distance_meters": float("inf")},
        ):
            self.assertEqual(
                request_motion_intent(self.socket_path, "move", timeout=2.0, **parameters),
                {"ok": False, "error": "invalid_distance"},
            )

    def test_diagnostic_turn_parameters_arrive_at_main_loop(self):
        thread, holder = self._send_request_threaded(
            "diagnostic_turn",
            direction="toward_left_wheel",
            duration_seconds=0.5,
        )

        deadline = time.monotonic() + 2.0
        pending = None
        while pending is None and time.monotonic() < deadline:
            pending = self.bridge.take_pending()
            if pending is None:
                time.sleep(0.01)
        self.assertIsNotNone(pending)

        request, complete = pending
        self.assertEqual(
            request,
            {
                "tool": "diagnostic_turn",
                "direction": "toward_left_wheel",
                "duration_seconds": 0.5,
            },
        )
        complete({"ok": True, "result": "completed"})
        thread.join(timeout=2.0)
        self.assertEqual(holder[0], {"ok": True, "result": "completed"})

    def test_diagnostic_turn_rejects_invalid_parameters_at_socket(self):
        self.assertEqual(
            request_motion_intent(
                self.socket_path,
                "diagnostic_turn",
                timeout=2.0,
                direction="left",
                duration_seconds=0.5,
            ),
            {"ok": False, "error": "invalid_direction"},
        )
        self.assertEqual(
            request_motion_intent(
                self.socket_path,
                "diagnostic_turn",
                timeout=2.0,
                direction="toward_left_wheel",
                duration_seconds=DIAGNOSTIC_TURN_MAX_DURATION + 0.1,
            ),
            {"ok": False, "error": "invalid_duration"},
        )

    def test_turn_parameters_arrive_at_main_loop(self):
        thread, holder = self._send_request_threaded("turn", degrees=-120)

        deadline = time.monotonic() + 2.0
        pending = None
        while pending is None and time.monotonic() < deadline:
            pending = self.bridge.take_pending()
            if pending is None:
                time.sleep(0.01)
        self.assertIsNotNone(pending)

        request, complete = pending
        self.assertEqual(request, {"tool": "turn", "degrees": -120})
        complete({"ok": True, "result": "completed"})
        thread.join(timeout=2.0)
        self.assertEqual(holder[0], {"ok": True, "result": "completed"})

    def test_turn_rejects_non_numeric_degrees_at_socket(self):
        # The socket only type-checks; out-of-range magnitudes are clamped later by
        # the executor, so only a non-numeric degrees value is rejected here. NaN and
        # inf survive json round-trips but never finish a timed turn, so reject them too.
        self.assertEqual(
            request_motion_intent(self.socket_path, "turn", timeout=2.0, degrees="lots"),
            {"ok": False, "error": "invalid_degrees"},
        )
        self.assertEqual(
            request_motion_intent(self.socket_path, "turn", timeout=2.0, degrees=float("nan")),
            {"ok": False, "error": "invalid_degrees"},
        )

    def test_face_me_parameters_arrive_at_main_loop(self):
        thread, holder = self._send_request_threaded("face_me", relative_degrees=85)

        deadline = time.monotonic() + 2.0
        pending = None
        while pending is None and time.monotonic() < deadline:
            pending = self.bridge.take_pending()
            if pending is None:
                time.sleep(0.01)
        self.assertIsNotNone(pending)

        request, complete = pending
        self.assertEqual(request, {"tool": "face_me", "relative_degrees": 85})
        complete({"ok": True, "result": "completed"})
        thread.join(timeout=2.0)
        self.assertEqual(holder[0], {"ok": True, "result": "completed"})

    def test_face_me_accepts_range_endpoints_at_socket(self):
        for value in (-180, 0, 180):
            thread, holder = self._send_request_threaded("face_me", relative_degrees=value)

            deadline = time.monotonic() + 2.0
            pending = None
            while pending is None and time.monotonic() < deadline:
                pending = self.bridge.take_pending()
                if pending is None:
                    time.sleep(0.01)
            self.assertIsNotNone(pending)

            _, complete = pending
            complete({"ok": True, "result": "completed"})
            thread.join(timeout=2.0)
            self.assertEqual(holder[0], {"ok": True, "result": "completed"})

    def test_face_me_rejects_invalid_relative_degrees_at_socket(self):
        for value in ({}, "90", True, 181, -181):
            parameters = {} if value == {} else {"relative_degrees": value}
            self.assertEqual(
                request_motion_intent(self.socket_path, "face_me", timeout=2.0, **parameters),
                {"ok": False, "error": "invalid_relative_degrees"},
            )

    def test_stop_replies_immediately_and_sets_flag(self):
        result = request_motion_intent(self.socket_path, "stop", timeout=2.0)
        self.assertEqual(result, {"ok": True, "result": "stopping"})
        self.assertTrue(self.bridge.take_stop())
        self.assertFalse(self.bridge.take_stop())

    def test_stop_replies_while_a_motion_request_is_still_in_flight(self):
        # Occupy the single pending slot with a move that never completes, mirroring a
        # real drive that is mid-motion. The stop must still come straight back.
        move_thread, _ = self._send_request_threaded("move", distance_meters=2.0)

        deadline = time.monotonic() + 2.0
        pending = None
        while pending is None and time.monotonic() < deadline:
            pending = self.bridge.take_pending()
            if pending is None:
                time.sleep(0.01)
        self.assertIsNotNone(pending)

        result = request_motion_intent(self.socket_path, "stop", timeout=2.0)
        self.assertEqual(result, {"ok": True, "result": "stopping"})
        self.assertTrue(self.bridge.take_stop())

        # Release the in-flight move so its client thread can finish cleanly.
        _, complete = pending
        complete({"ok": False, "error": "stopped"})
        move_thread.join(timeout=2.0)

    def test_discard_pending_drops_queued_request_with_stopped(self):
        thread, holder = self._send_request_threaded("move", distance_meters=1.0)

        deadline = time.monotonic() + 2.0
        while self.bridge._pending is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(self.bridge._pending)

        self.bridge.discard_pending()
        thread.join(timeout=2.0)
        self.assertEqual(holder[0], {"ok": False, "error": "stopped"})

    def test_express_rejects_invalid_kind_at_socket(self):
        self.assertEqual(
            request_motion_intent(self.socket_path, "express", timeout=2.0, kind="nod"),
            {"ok": False, "error": "invalid_kind"},
        )

    def test_completion_failure_result_reaches_client(self):
        thread, holder = self._send_request_threaded("move", distance_meters=1.0)

        deadline = time.monotonic() + 2.0
        pending = None
        while pending is None and time.monotonic() < deadline:
            pending = self.bridge.take_pending()
            if pending is None:
                time.sleep(0.01)
        self.assertIsNotNone(pending)

        _, complete = pending
        complete({"ok": False, "error": "preempted_by_gamepad"})

        thread.join(timeout=2.0)
        self.assertEqual(holder[0], {"ok": False, "error": "preempted_by_gamepad"})


class MotionIntentClientErrorTest(unittest.TestCase):
    def test_missing_socket_returns_motion_socket_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = os.path.join(tmpdir, "nope.sock")
            result = request_motion_intent(missing, "wiggle", timeout=0.5)
        self.assertEqual(result, {"ok": False, "error": "motion_socket_missing"})

    def test_stale_socket_returns_motion_socket_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = os.path.join(tmpdir, "stale.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(socket_path)
            server.close()
            result = request_motion_intent(socket_path, "wiggle", timeout=0.5)
        self.assertEqual(result, {"ok": False, "error": "motion_socket_refused"})

    def test_no_response_when_server_closes_silently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = os.path.join(tmpdir, "silent.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(socket_path)
            server.listen(1)

            def accept_and_close():
                conn, _ = server.accept()
                # Drain the request so the close happens after send completes —
                # this exercises the "server replied with nothing" path.
                conn.recv(4096)
                conn.close()

            thread = threading.Thread(target=accept_and_close, daemon=True)
            thread.start()
            try:
                result = request_motion_intent(socket_path, "wiggle", timeout=1.0)
            finally:
                thread.join(timeout=1.0)
                server.close()

        self.assertEqual(result, {"ok": False, "error": "no_response"})


class MotionToolDispatchTest(unittest.TestCase):
    def test_motion_tool_calls_caller_and_feeds_result_back(self):
        async def run():
            calls: list[tuple[str, dict]] = []

            def fake_caller(tool, **params):
                calls.append((tool, params))
                return {"ok": True, "result": "completed"}

            class FakeResponses:
                def __init__(self):
                    self.requests: list[dict] = []

                async def create(self, **kwargs):
                    self.requests.append(kwargs)
                    if len(self.requests) == 1:
                        events = [
                            SimpleNamespace(
                                type="response.output_item.done",
                                item=SimpleNamespace(
                                    type="function_call",
                                    name="express",
                                    arguments='{"kind": "wiggle"}',
                                    call_id="call_1",
                                ),
                            ),
                            SimpleNamespace(
                                type="response.completed",
                                response=SimpleNamespace(id="resp_1"),
                            ),
                        ]
                    else:
                        events = [
                            SimpleNamespace(type="response.output_text.delta", delta="Wiggled."),
                            SimpleNamespace(
                                type="response.completed",
                                response=SimpleNamespace(id="resp_2"),
                            ),
                        ]

                    async def stream():
                        for event in events:
                            yield event

                    return stream()

            fake_responses = FakeResponses()

            chunks = [
                chunk
                async for chunk in stream_openai_words(
                    [{"role": "user", "content": "Wiggle please"}],
                    SimpleNamespace(responses=fake_responses),
                    VoiceState("test-voice"),
                    motion_intent_caller=fake_caller,
                )
            ]

            self.assertEqual(calls, [("express", {"kind": "wiggle"})])
            self.assertEqual(chunks, ["Wiggled."])
            second_input = fake_responses.requests[1]["input"]
            self.assertEqual(second_input[0]["type"], "function_call_output")
            self.assertEqual(second_input[0]["call_id"], "call_1")
            self.assertEqual(
                json.loads(second_input[0]["output"]),
                {"ok": True, "result": "completed"},
            )

        asyncio.run(run())

    def test_motion_tool_with_no_caller_reports_motion_caller_missing(self):
        async def run():
            class FakeResponses:
                def __init__(self):
                    self.requests: list[dict] = []

                async def create(self, **kwargs):
                    self.requests.append(kwargs)
                    if len(self.requests) == 1:
                        events = [
                            SimpleNamespace(
                                type="response.output_item.done",
                                item=SimpleNamespace(
                                    type="function_call",
                                    name="move",
                                    call_id="call_x",
                                ),
                            ),
                            SimpleNamespace(
                                type="response.completed",
                                response=SimpleNamespace(id="resp_1"),
                            ),
                        ]
                    else:
                        events = [
                            SimpleNamespace(type="response.output_text.delta", delta="Sorry."),
                            SimpleNamespace(
                                type="response.completed",
                                response=SimpleNamespace(id="resp_2"),
                            ),
                        ]

                    async def stream():
                        for event in events:
                            yield event

                    return stream()

            fake_responses = FakeResponses()

            chunks = [
                chunk
                async for chunk in stream_openai_words(
                    [{"role": "user", "content": "Move forward"}],
                    SimpleNamespace(responses=fake_responses),
                    VoiceState("test-voice"),
                )
            ]

            self.assertEqual(chunks, ["Sorry."])
            second_input = fake_responses.requests[1]["input"]
            self.assertEqual(
                json.loads(second_input[0]["output"]),
                {"ok": False, "error": "motion_caller_missing"},
            )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
