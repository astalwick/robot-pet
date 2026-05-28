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
    MOVE_FORWARD_DURATION,
    MOVE_FORWARD_LINEAR_X,
    WIGGLE_ANGULAR_Z,
    WIGGLE_HALF_DURATION,
    MotionIntentBridge,
    MotionIntentExecutor,
    request_motion_intent,
)
from voice.assistant import stream_openai_words


class MotionIntentExecutorTest(unittest.TestCase):
    def test_unknown_tool_rejected(self):
        executor = MotionIntentExecutor()
        self.assertEqual(executor.start("spin", now=0.0), "unknown_tool")
        self.assertFalse(executor.is_active())

    def test_second_request_while_busy_rejected(self):
        executor = MotionIntentExecutor()
        self.assertIsNone(executor.start("wiggle", now=0.0))
        self.assertEqual(executor.start("wiggle", now=0.05), "busy")

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


class MotionIntentBridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmpdir.name, "motion.sock")
        self.bridge = MotionIntentBridge(self.socket_path)
        self.bridge.start()

    def tearDown(self):
        self.bridge.stop()
        self.tmpdir.cleanup()

    def _send_request_threaded(self, tool):
        result_holder: list[dict] = []

        def worker():
            result_holder.append(request_motion_intent(self.socket_path, tool, timeout=2.0))

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

        tool, complete = pending
        self.assertEqual(tool, "wiggle")
        complete({"ok": True, "result": "completed"})

        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(holder[0], {"ok": True, "result": "completed"})

    def test_unknown_tool_rejected_at_socket(self):
        result = request_motion_intent(self.socket_path, "spin", timeout=2.0)
        self.assertEqual(result, {"ok": False, "error": "unknown_tool"})

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
