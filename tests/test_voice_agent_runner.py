import asyncio
import json
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from voice.agent_runner import STEP_LIMIT_FINAL, TIMEOUT_FINAL, run_agent_goal
from voice.assistant import VoiceState


class FakeResponse:
    def __init__(self, text: str):
        self.output_text = text


class FakeResponses:
    """Scripts the model. Each `create` calls a responder with the current input."""

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[dict] = []
        self.delay = 0.0

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        return FakeResponse(self.responder(kwargs["input"]))


class FakeOpenAI:
    def __init__(self, responder):
        self.responses = FakeResponses(responder)


def decision(**fields) -> str:
    payload = {"narration": "", "tool_calls": [], "done": False, "blocked": False, "final": None}
    payload.update(fields)
    return json.dumps(payload)


def call_tool(name: str) -> dict:
    return {"name": name, "arguments": {}}


def serialized(input_items) -> str:
    return json.dumps(input_items)


def run(coro):
    return asyncio.run(coro)


class AgentRunnerTest(unittest.TestCase):
    def test_repeated_move_forward_until_observation_makes_it_done(self):
        moves: list[str] = []

        def responder(_input_items):
            # The model keeps nudging forward and stops once it has observed three
            # move_forward results come back.
            if len(moves) >= 3:
                return decision(done=True, final="I am right next to you now.")
            return decision(tool_calls=[call_tool("move_forward")])

        openai = FakeOpenAI(responder)
        result = run(
            run_agent_goal(
                goal="Move toward me and stop when you are close.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=lambda name: moves.append(name) or {"ok": True},
            )
        )

        self.assertEqual(result, "I am right next to you now.")
        self.assertEqual(moves, ["move_forward", "move_forward", "move_forward"])
        self.assertGreater(len(openai.responses.calls), 3)

    def test_look_around_image_is_sent_as_image_input_not_json(self):
        snapshots: list[bool] = []

        def responder(_input_items):
            if snapshots:
                return decision(done=True, final="I see the ball by the couch.")
            return decision(tool_calls=[call_tool("look_around")])

        def snapshot():
            snapshots.append(True)
            return b"jpeg-bytes"

        openai = FakeOpenAI(responder)
        result = run(
            run_agent_goal(
                goal="Tell me what you see.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                camera_snapshot_caller=snapshot,
            )
        )

        self.assertEqual(result, "I see the ball by the couch.")
        follow_up_input = openai.responses.calls[1]["input"]
        observation = next(item for item in follow_up_input if isinstance(item["content"], list))
        image_parts = [part for part in observation["content"] if part.get("type") == "input_image"]
        self.assertEqual(len(image_parts), 1)
        self.assertTrue(image_parts[0]["image_url"].startswith("data:image/jpeg;base64,"))
        # The base64 image must not be stuffed into the JSON status text.
        text_parts = [part for part in observation["content"] if part.get("type") == "input_text"]
        self.assertNotIn("base64", text_parts[0]["text"])

    def test_motion_is_awaited_before_inspect_robot_in_same_step(self):
        order: list[str] = []

        def slow_move(_name):
            # motion_intent_caller is sync and runs via asyncio.to_thread; emulate
            # the motion intent taking real wall-clock time to finish.
            order.append("move_start")
            time.sleep(0.02)
            order.append("move_done")
            return {"ok": True}

        def inspect():
            order.append("inspect")
            return {"drive_status": {"state": "idle"}}

        responder_calls = {"n": 0}

        def responder(_input_items):
            responder_calls["n"] += 1
            if responder_calls["n"] == 1:
                return decision(tool_calls=[call_tool("move_forward"), call_tool("inspect_robot")])
            return decision(done=True, final="Done.")

        openai = FakeOpenAI(responder)
        run(
            run_agent_goal(
                goal="Step forward and check.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=slow_move,
                robot_inspection_caller=inspect,
            )
        )

        self.assertEqual(order, ["move_start", "move_done", "inspect"])

    def test_tool_failure_becomes_observation_and_model_recovers(self):
        failures: list[bool] = []

        def blocked_move(_name):
            failures.append(True)
            return {"ok": False, "error": "safety_blocked"}

        def responder(_input_items):
            if failures:
                return decision(blocked=True, final="The drive is blocked, so I stopped.")
            return decision(tool_calls=[call_tool("move_forward")])

        openai = FakeOpenAI(responder)
        result = run(
            run_agent_goal(
                goal="Move forward.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=blocked_move,
            )
        )

        self.assertEqual(result, "The drive is blocked, so I stopped.")

    def test_unknown_tool_name_becomes_observation_not_crash(self):
        def responder(input_items):
            if "unknown tool" in serialized(input_items):
                return decision(done=True, final="Okay, I will skip that.")
            return decision(tool_calls=[call_tool("teleport")])

        openai = FakeOpenAI(responder)
        result = run(
            run_agent_goal(
                goal="Do the thing.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
            )
        )

        self.assertEqual(result, "Okay, I will skip that.")

    def test_step_limit_returns_spoken_final(self):
        openai = FakeOpenAI(lambda _input: decision(tool_calls=[call_tool("move_forward")]))
        result = run(
            run_agent_goal(
                goal="Keep going.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=lambda _name: {"ok": True},
                max_steps=2,
            )
        )

        self.assertEqual(result, STEP_LIMIT_FINAL)
        self.assertEqual(len(openai.responses.calls), 2)

    def test_timeout_during_model_call_returns_final_without_running_tools(self):
        moves: list[str] = []
        openai = FakeOpenAI(lambda _input: decision(tool_calls=[call_tool("move_forward")]))
        # The model call itself overruns the budget; the decision it eventually
        # returns must not be acted on.
        openai.responses.delay = 0.06
        result = run(
            run_agent_goal(
                goal="Keep going.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=lambda name: moves.append(name) or {"ok": True},
                max_seconds=0.05,
            )
        )

        self.assertEqual(result, TIMEOUT_FINAL)
        self.assertEqual(moves, [])

    def test_stop_event_cancels_the_goal(self):
        stop_event = asyncio.Event()
        stop_event.set()
        openai = FakeOpenAI(lambda _input: decision(tool_calls=[call_tool("move_forward")]))
        result = run(
            run_agent_goal(
                goal="Keep going.",
                stop_event=stop_event,
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=lambda _name: {"ok": True},
            )
        )

        self.assertEqual(result, "")
        self.assertEqual(openai.responses.calls, [])


if __name__ == "__main__":
    unittest.main()
