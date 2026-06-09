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
    FACE_ME_MAX_RELATIVE_DEGREES,
    MOVE_FORWARD_DURATION,
    MOVE_FORWARD_LINEAR_X,
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
        self.assertIsNone(executor.start("wiggle", now=0.0))
        self.assertEqual(executor.start("wiggle", now=0.05), "busy")

    def test_cancel_clears_active_intent(self):
        executor = MotionIntentExecutor()
        executor.start("wiggle", now=0.0)
        self.assertTrue(executor.is_active())

        executor.cancel()

        self.assertFalse(executor.is_active())

    def test_reset_active_start_restarts_elapsed_time(self):
        executor = MotionIntentExecutor()
        executor.start("wiggle", now=0.0)

        executor.reset_active_start(now=2.0)
        tick = executor.tick(now=2.1, gamepad_active=False)

        self.assertEqual(tick.command, MotionCommand(0.0, WIGGLE_ANGULAR_Z))
        self.assertFalse(tick.finished)

    def test_wiggle_emits_left_then_right_then_completes(self):
        executor = MotionIntentExecutor()
        executor.start("wiggle", now=0.0)

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

    def test_move_forward_runs_for_duration_then_completes(self):
        executor = MotionIntentExecutor()
        executor.start("move_forward", now=0.0)

        running = executor.tick(now=0.1, gamepad_active=False)
        self.assertEqual(running.command, MotionCommand(MOVE_FORWARD_LINEAR_X, 0.0))
        self.assertFalse(running.finished)

        done = executor.tick(now=MOVE_FORWARD_DURATION + 0.05, gamepad_active=False)
        self.assertTrue(done.finished)
        self.assertEqual(done.result, "completed")

    def test_gamepad_active_preempts_mid_intent(self):
        executor = MotionIntentExecutor()
        executor.start("move_forward", now=0.0)

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

        # 90 / 55 == 1.636 seconds.
        done = executor.tick(now=90 / FACE_ME_DEGREES_PER_SECOND + 0.05, gamepad_active=False)
        self.assertTrue(done.finished)
        self.assertEqual(done.result, "completed")

    def test_face_me_negative_turns_toward_right_wheel(self):
        executor = MotionIntentExecutor()
        executor.start("face_me", now=0.0, relative_degrees=-90)

        running = executor.tick(now=0.5, gamepad_active=False)
        self.assertEqual(running.command, MotionCommand(0.0, FACE_ME_ANGULAR_Z))

    def test_face_me_duration_uses_fifty_five_degrees_per_second(self):
        executor = MotionIntentExecutor()
        executor.start("face_me", now=0.0, relative_degrees=110)

        # 110 / 55 == 2.0 seconds: still turning just before, done just after.
        self.assertFalse(executor.tick(now=1.95, gamepad_active=False).finished)
        self.assertTrue(executor.tick(now=2.05, gamepad_active=False).finished)

    def test_face_me_within_fifteen_degrees_completes_without_moving(self):
        executor = MotionIntentExecutor()
        executor.start("face_me", now=0.0, relative_degrees=15)

        tick = executor.tick(now=0.0, gamepad_active=False)
        self.assertTrue(tick.finished)
        self.assertEqual(tick.result, "completed")
        self.assertIsNone(tick.command)

    def test_face_me_full_turn_stays_below_four_second_maximum(self):
        executor = MotionIntentExecutor()
        executor.start("face_me", now=0.0, relative_degrees=FACE_ME_MAX_RELATIVE_DEGREES)

        # 180 / 55 == 3.27 seconds, still under the 4.0s bounded-turn limit.
        self.assertFalse(executor.tick(now=3.2, gamepad_active=False).finished)
        self.assertTrue(executor.tick(now=3.3, gamepad_active=False).finished)
        self.assertLess(FACE_ME_MAX_RELATIVE_DEGREES / FACE_ME_DEGREES_PER_SECOND, 4.0)

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
        thread, holder = self._send_request_threaded("wiggle")

        deadline = time.monotonic() + 2.0
        pending = None
        while pending is None and time.monotonic() < deadline:
            pending = self.bridge.take_pending()
            if pending is None:
                time.sleep(0.01)
        self.assertIsNotNone(pending)

        request, complete = pending
        self.assertEqual(request, {"tool": "wiggle"})
        complete({"ok": True, "result": "completed"})

        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(holder[0], {"ok": True, "result": "completed"})

    def test_unknown_tool_rejected_at_socket(self):
        result = request_motion_intent(self.socket_path, "spin", timeout=2.0)
        self.assertEqual(result, {"ok": False, "error": "unknown_tool"})

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

    def test_completion_failure_result_reaches_client(self):
        thread, holder = self._send_request_threaded("move_forward")

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
            calls: list[str] = []

            def fake_caller(tool):
                calls.append(tool)
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
                                    type="function_call", name="wiggle", call_id="call_1"
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

            self.assertEqual(calls, ["wiggle"])
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
                                    name="move_forward",
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
