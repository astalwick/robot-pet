import asyncio
import json
import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from voice.agent_runner import (
    AGENT_TOOLS,
    BLOCKED_FINAL,
    NUDGE_TEXT,
    STEP_LIMIT_FINAL,
    TIMEOUT_FINAL,
    goal_pose_text,
    run_agent_goal,
)
from voice.assistant import VoiceState


class FakeFunctionCall:
    """One native `function_call` output item, as the Responses API returns it."""

    def __init__(self, name: str, arguments: str, call_id: str):
        self.type = "function_call"
        self.name = name
        self.arguments = arguments
        self.call_id = call_id


class FakeResponse:
    def __init__(self, output: list, output_text: str = "", response_id: str | None = None):
        self.output = output
        self.output_text = output_text
        self.id = response_id


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
        response = self.responder(kwargs["input"])
        if response.id is None:
            response.id = f"resp_{len(self.calls)}"
        return response


class FakeOpenAI:
    def __init__(self, responder):
        self.responses = FakeResponses(responder)


def call(name: str, *, call_id: str = "call_1", arguments: dict | None = None, narration: str = "") -> FakeResponse:
    """A response that makes one native tool call, optionally with progress narration."""
    function_call = FakeFunctionCall(name, json.dumps(arguments or {}), call_id)
    return FakeResponse(output=[function_call], output_text=narration)


def final(text: str) -> FakeResponse:
    """A response with no tool call: its text is the final answer."""
    return FakeResponse(output=[], output_text=text)


def empty() -> FakeResponse:
    """A response with no tool call and no words."""
    return FakeResponse(output=[], output_text="")


def serialized(input_items) -> str:
    return json.dumps(input_items)


def run(coro):
    return asyncio.run(coro)


class NativeToolMechanicsTest(unittest.TestCase):
    def test_request_includes_native_tools_and_no_parallel_calls(self):
        openai = FakeOpenAI(lambda _input: final("Hi."))
        run(
            run_agent_goal(
                goal="Say hi.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
            )
        )

        first = openai.responses.calls[0]
        self.assertEqual(first["tools"], AGENT_TOOLS)
        self.assertIs(first["parallel_tool_calls"], False)

    def test_move_runs_with_arguments_parsed_from_json_string(self):
        captured: dict = {}
        timeline_events = []
        steps = {"n": 0}

        def responder(_input_items):
            steps["n"] += 1
            if steps["n"] == 1:
                return call("move", arguments={"distance_meters": 0.5})
            return final("Done.")

        def move(_name, **kwargs):
            captured.update(kwargs)
            return {"ok": True}

        openai = FakeOpenAI(responder)
        run(
            run_agent_goal(
                goal="Move.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=move,
                on_event=timeline_events.append,
            )
        )

        self.assertEqual(captured["distance_meters"], 0.5)
        self.assertEqual(
            [(event["type"], event["source"], event["name"], event.get("ok")) for event in timeline_events],
            [("tool_start", "goal", "move", None), ("tool_done", "goal", "move", True)],
        )
        self.assertEqual(timeline_events[0]["args"], {"distance_meters": 0.5})
        self.assertEqual(timeline_events[1]["started_at"], timeline_events[0]["t"])
        self.assertGreaterEqual(timeline_events[1]["duration_ms"], 0)

    def test_tool_output_goes_back_with_matching_call_id(self):
        steps = {"n": 0}

        def responder(_input_items):
            steps["n"] += 1
            if steps["n"] == 1:
                return call("move", call_id="call_42", arguments={"distance_meters": 0.5})
            return final("Done.")

        openai = FakeOpenAI(responder)
        run(
            run_agent_goal(
                goal="Move.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=lambda _name, **_: {"ok": True},
            )
        )

        observation = openai.responses.calls[1]["input"][0]
        self.assertEqual(observation["type"], "function_call_output")
        self.assertEqual(observation["call_id"], "call_42")

    def test_repeated_move_until_observation_makes_it_done(self):
        moves: list[str] = []

        def responder(_input_items):
            if len(moves) >= 3:
                return final("I am right next to you now.")
            return call("move", arguments={"distance_meters": 0.5})

        openai = FakeOpenAI(responder)
        result = run(
            run_agent_goal(
                goal="Move toward me and stop when you are close.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=lambda name, **_: moves.append(name) or {"ok": True},
            )
        )

        self.assertEqual(result, "I am right next to you now.")
        self.assertEqual(moves, ["move", "move", "move"])
        self.assertGreater(len(openai.responses.calls), 3)

    def test_look_image_is_sent_as_image_input_not_json(self):
        snapshots: list[bool] = []

        def responder(_input_items):
            if snapshots:
                return final("I see the ball by the couch.")
            return call("look")

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
        observation = next(item for item in follow_up_input if isinstance(item.get("content"), list))
        image_parts = [part for part in observation["content"] if part.get("type") == "input_image"]
        self.assertEqual(len(image_parts), 1)
        self.assertTrue(image_parts[0]["image_url"].startswith("data:image/jpeg;base64,"))
        # The base64 image must not be stuffed into the JSON status text.
        text_parts = [part for part in observation["content"] if part.get("type") == "input_text"]
        self.assertNotIn("base64", text_parts[0]["text"])

    def test_inspect_speaker_direction_observation_reaches_the_model(self):
        def speaker_direction():
            return {"connected": True, "relative_degrees": 90, "age_seconds": 0.1, "fresh": True}

        def responder(input_items):
            if "relative_degrees" in serialized(input_items):
                return final("You are on my right.")
            return call("inspect_speaker_direction")

        openai = FakeOpenAI(responder)
        result = run(
            run_agent_goal(
                goal="Which way am I?",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                speaker_direction_caller=speaker_direction,
            )
        )

        self.assertEqual(result, "You are on my right.")

    def test_tool_failure_becomes_observation_and_model_recovers(self):
        failures: list[bool] = []

        def blocked_move(_name, **_):
            failures.append(True)
            return {"ok": False, "error": "safety_blocked"}

        def responder(_input_items):
            if failures:
                return final("The drive is blocked, so I stopped.")
            return call("move", arguments={"distance_meters": 0.5})

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
            if "unsupported tool" in serialized(input_items):
                return final("Okay, I will skip that.")
            return call("teleport")

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


class ReasonsWellTest(unittest.TestCase):
    def test_each_call_after_first_chains_and_sends_only_new_output(self):
        steps = {"n": 0}

        def responder(_input_items):
            steps["n"] += 1
            if steps["n"] == 1:
                return call("move", call_id="call_x", arguments={"distance_meters": 0.5})
            return final("Done.")

        openai = FakeOpenAI(responder)
        run(
            run_agent_goal(
                goal="Move.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=lambda _name, **_: {"ok": True},
            )
        )

        first, second = openai.responses.calls[0], openai.responses.calls[1]
        # The first call has no chain and carries the developer prompt and goal.
        self.assertNotIn("previous_response_id", first)
        self.assertTrue(any(item.get("role") == "developer" for item in first["input"]))
        # The second call chains on the first response and carries only the new
        # observation, not a rebuilt transcript.
        self.assertEqual(second["previous_response_id"], "resp_1")
        self.assertEqual(len(second["input"]), 2)
        self.assertEqual(second["input"][0]["type"], "function_call_output")
        self.assertEqual(second["input"][0]["call_id"], "call_x")
        self.assertIn("Position:", second["input"][1]["content"])

    def test_natural_termination_speaks_final_with_no_nudge(self):
        spoken: list[str] = []

        async def speak_progress(text):
            spoken.append(text)

        openai = FakeOpenAI(lambda _input: final("All set."))
        result = run(
            run_agent_goal(
                goal="Say something.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                speak_progress=speak_progress,
                is_speaking=lambda: False,
            )
        )

        self.assertEqual(result, "All set.")
        self.assertEqual(spoken, ["All set."])
        self.assertEqual(len(openai.responses.calls), 1)

    def test_empty_responses_nudge_then_finish(self):
        steps = {"n": 0}

        def responder(_input_items):
            steps["n"] += 1
            if steps["n"] <= 2:
                return empty()
            return final("Okay, finished.")

        openai = FakeOpenAI(responder)
        result = run(
            run_agent_goal(
                goal="Do it.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
            )
        )

        self.assertEqual(result, "Okay, finished.")
        nudges = [c for c in openai.responses.calls if NUDGE_TEXT in serialized(c["input"])]
        self.assertEqual(len(nudges), 2)

    def test_all_empty_responses_eventually_blocks(self):
        openai = FakeOpenAI(lambda _input: empty())
        result = run(
            run_agent_goal(
                goal="Do it.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
            )
        )

        self.assertEqual(result, BLOCKED_FINAL)

    def test_normal_run_injects_no_bookkeeping_or_wrap_up(self):
        moves: list[str] = []

        def responder(_input_items):
            if len(moves) >= 3:
                return final("Reached you.")
            return call("move", arguments={"distance_meters": 0.5})

        openai = FakeOpenAI(responder)
        run(
            run_agent_goal(
                goal="Come here.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=lambda name, **_: moves.append(name) or {"ok": True},
            )
        )

        # A healthy multi-step run never injects a nudge or a "wrap up soon" line.
        for request in openai.responses.calls:
            self.assertNotIn(NUDGE_TEXT, serialized(request["input"]))
            self.assertNotIn("wrap up", serialized(request["input"]).lower())


class SpeechConcurrencyTest(unittest.TestCase):
    def test_text_alongside_tool_call_is_narrated_not_the_final(self):
        spoken: list[str] = []

        async def speak_progress(text):
            spoken.append(text)

        steps = {"n": 0}

        def responder(_input_items):
            steps["n"] += 1
            if steps["n"] >= 2:
                return final("Reached the goal.")
            return call("move", narration="Heading over now.", arguments={"distance_meters": 0.5})

        openai = FakeOpenAI(responder)
        result = run(
            run_agent_goal(
                goal="Come here.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=lambda _name, **_: {"ok": True},
                speak_progress=speak_progress,
                is_speaking=lambda: False,
            )
        )

        # The narration was spoken, but only the no-tool-call text is the final the
        # runner returns (and thus the only text the caller commits to history).
        self.assertEqual(result, "Reached the goal.")
        self.assertEqual(spoken, ["Heading over now.", "Reached the goal."])

    def test_narration_does_not_block_the_next_tool(self):
        order: list[str] = []

        async def speak_progress(_text):
            order.append("narrate_start")
            await asyncio.sleep(0.02)
            order.append("narrate_done")

        steps = {"n": 0}

        def responder(_input_items):
            steps["n"] += 1
            if steps["n"] >= 2:
                return final("Done.")
            return call("move", narration="On my way.", arguments={"distance_meters": 0.5})

        def move(_name, **_):
            order.append("tool")
            return {"ok": True}

        openai = FakeOpenAI(responder)
        run(
            run_agent_goal(
                goal="Move.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=move,
                speak_progress=speak_progress,
                is_speaking=lambda: False,
            )
        )

        # The tool ran while narration was still playing, not after it finished.
        self.assertIn("narrate_start", order)
        self.assertLess(order.index("tool"), order.index("narrate_done"))

    def test_narration_skipped_while_already_speaking(self):
        spoken: list[str] = []

        async def speak_progress(text):
            spoken.append(text)

        steps = {"n": 0}

        def responder(_input_items):
            steps["n"] += 1
            if steps["n"] >= 2:
                return final("Done.")
            return call("move", narration="On my way.", arguments={"distance_meters": 0.5})

        openai = FakeOpenAI(responder)
        run(
            run_agent_goal(
                goal="Move.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=lambda _name, **_: {"ok": True},
                speak_progress=speak_progress,
                is_speaking=lambda: True,
            )
        )

        # Interim narration is suppressed while speaking; the final is always said.
        self.assertEqual(spoken, ["Done."])

    def test_final_speech_does_not_overlap_with_progress_speech(self):
        concurrency = {"now": 0, "max": 0}

        async def speak_progress(_text):
            concurrency["now"] += 1
            concurrency["max"] = max(concurrency["max"], concurrency["now"])
            await asyncio.sleep(0.01)
            concurrency["now"] -= 1

        steps = {"n": 0}

        def responder(_input_items):
            steps["n"] += 1
            if steps["n"] >= 2:
                return final("Done.")
            return call("move", narration="Working on it.", arguments={"distance_meters": 0.5})

        openai = FakeOpenAI(responder)
        run(
            run_agent_goal(
                goal="Move.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=lambda _name, **_: {"ok": True},
                speak_progress=speak_progress,
                is_speaking=lambda: False,
            )
        )

        self.assertEqual(concurrency["max"], 1)

    def test_narration_failure_does_not_sink_the_final(self):
        spoken: list[str] = []

        async def speak_progress(text):
            if text == "On my way.":
                await asyncio.sleep(0.01)
                raise RuntimeError("tts socket died")
            spoken.append(text)

        steps = {"n": 0}

        def responder(_input_items):
            steps["n"] += 1
            if steps["n"] >= 2:
                return final("Reached the goal.")
            return call("move", narration="On my way.", arguments={"distance_meters": 0.5})

        openai = FakeOpenAI(responder)
        result = run(
            run_agent_goal(
                goal="Move.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=lambda _name, **_: {"ok": True},
                speak_progress=speak_progress,
                is_speaking=lambda: False,
            )
        )

        # The narration blew up mid-flight, but the goal still finished and spoke
        # its final line.
        self.assertEqual(result, "Reached the goal.")
        self.assertEqual(spoken, ["Reached the goal."])

    def test_stop_cancels_the_goal_and_in_flight_narration(self):
        stop_event = asyncio.Event()
        cancelled = {"value": False}

        async def speak_progress(_text):
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                cancelled["value"] = True
                raise

        def move(_name, **_):
            stop_event.set()
            return {"ok": True}

        openai = FakeOpenAI(lambda _input: call("move", narration="Working.", arguments={"distance_meters": 0.5}))
        result = run(
            run_agent_goal(
                goal="Move.",
                stop_event=stop_event,
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=move,
                speak_progress=speak_progress,
                is_speaking=lambda: False,
            )
        )

        self.assertEqual(result, "")
        self.assertTrue(cancelled["value"])


class GuardRailsTest(unittest.TestCase):
    def test_step_limit_returns_spoken_final(self):
        openai = FakeOpenAI(lambda _input: call("move", arguments={"distance_meters": 0.5}))
        result = run(
            run_agent_goal(
                goal="Keep going.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=lambda _name, **_: {"ok": True},
                max_steps=2,
            )
        )

        self.assertEqual(result, STEP_LIMIT_FINAL)
        self.assertEqual(len(openai.responses.calls), 2)

    def test_timeout_during_model_call_returns_final_without_running_tools(self):
        moves: list[str] = []
        openai = FakeOpenAI(lambda _input: call("move", arguments={"distance_meters": 0.5}))
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
                motion_intent_caller=lambda name, **_: moves.append(name) or {"ok": True},
                max_seconds=0.05,
            )
        )

        self.assertEqual(result, TIMEOUT_FINAL)
        self.assertEqual(moves, [])

    def test_stop_event_set_before_start_runs_nothing(self):
        stop_event = asyncio.Event()
        stop_event.set()
        openai = FakeOpenAI(lambda _input: call("move", arguments={"distance_meters": 0.5}))
        result = run(
            run_agent_goal(
                goal="Keep going.",
                stop_event=stop_event,
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=lambda _name, **_: {"ok": True},
            )
        )

        self.assertEqual(result, "")
        self.assertEqual(openai.responses.calls, [])


class MotionObservationTest(unittest.TestCase):
    TELEMETRY_SNAPSHOT = {
        "sources": {
            "gamepad_teleop": {"stale": False},
            "sensors": {"stale": False},
        },
        "sensors": {
            "status": "ok",
            "readings": [{"name": "front_right", "distance_mm": 120, "ok": True}],
        },
    }

    def test_move_attaches_surroundings_hint_and_camera(self):
        steps = {"n": 0}

        def responder(_input_items):
            steps["n"] += 1
            if steps["n"] >= 2:
                return final("Backed up.")
            return call("move", arguments={"distance_meters": 0.5})

        def move(_name, **_):
            return {
                "ok": False,
                "error": "safety_blocked",
                "traveled_m": 0.31,
                "blocked_by": "front_right_obstacle",
            }

        openai = FakeOpenAI(responder)
        run(
            run_agent_goal(
                goal="Move forward.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=move,
                robot_inspection_caller=lambda: self.TELEMETRY_SNAPSHOT,
                camera_snapshot_caller=lambda: b"jpeg-bytes",
            )
        )

        follow_up = openai.responses.calls[1]["input"]
        tool_output = json.loads(follow_up[0]["output"])
        self.assertFalse(tool_output["ok"])
        self.assertEqual(tool_output["error"], "safety_blocked")
        self.assertTrue(tool_output["surroundings"]["sensors"]["available"])
        self.assertIn("right side", tool_output["hint"])

        image_message = next(item for item in follow_up if isinstance(item.get("content"), list))
        text_parts = [part for part in image_message["content"] if part.get("type") == "input_text"]
        image_parts = [part for part in image_message["content"] if part.get("type") == "input_image"]
        self.assertIn("0.31 meters forward", text_parts[0]["text"])
        self.assertIn("(blocked)", text_parts[0]["text"])
        self.assertIn("Position:", text_parts[0]["text"])
        self.assertTrue(image_parts[0]["image_url"].startswith("data:image/jpeg;base64,"))

    def test_turn_then_blocked_move_pose_in_next_input(self):
        steps = {"n": 0}

        def motion(name, **_kwargs):
            if name == "turn":
                return {"ok": True, "result": "completed", "measured_degrees": 28.0}
            return {
                "ok": False,
                "error": "safety_blocked",
                "traveled_m": 0.31,
                "blocked_by": "front_right_obstacle",
            }

        def responder(_input_items):
            steps["n"] += 1
            if steps["n"] == 1:
                return call("turn", arguments={"degrees": 30})
            if steps["n"] == 2:
                return call("move", arguments={"distance_meters": 0.5})
            return final("Done.")

        openai = FakeOpenAI(responder)
        run(
            run_agent_goal(
                goal="Turn and move.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=motion,
                camera_snapshot_caller=lambda: b"jpeg-bytes",
            )
        )

        follow_up = openai.responses.calls[2]["input"]
        image_message = next(item for item in follow_up if isinstance(item.get("content"), list))
        caption = next(
            part["text"] for part in image_message["content"] if part.get("type") == "input_text"
        )
        heading_rad = math.radians(28.0)
        expected_x = 0.31 * math.cos(heading_rad)
        expected_y = 0.31 * math.sin(heading_rad)
        self.assertIn(f"{expected_x:.2f} meters forward", caption)
        self.assertIn(f"{expected_y:.2f} meters left", caption)
        self.assertIn("28 degrees left of your starting heading", caption)
        self.assertIn("turned 30 left (measured 28)", caption)
        self.assertIn("moved 0.5 forward (blocked at 0.31, right side)", caption)

    def test_goal_pose_text_formats_position_and_actions(self):
        from voice.agent_runner import GoalPose

        pose = GoalPose(x=1.4, y=0.3, heading=85.0)
        pose.recent_actions = [
            "turned 30 left (measured 28)",
            "moved 0.5 forward (blocked at 0.31, right side)",
        ]
        text = goal_pose_text(pose)
        self.assertIn("1.40 meters forward and 0.30 meters left", text)
        self.assertIn("85 degrees left of your starting heading", text)
        self.assertIn("Recent actions: turned 30 left (measured 28)", text)

    def test_failed_turn_without_measurement_does_not_update_pose(self):
        from voice.agent_runner import GoalPose

        pose = GoalPose()
        pose.record_motion(
            "turn",
            {"degrees": 30},
            {"ok": False, "error": "motion_caller_missing"},
        )
        self.assertEqual(pose.heading, 0.0)
        self.assertEqual(pose.recent_actions, [])

    def test_stalled_turn_with_measurement_still_updates_pose(self):
        from voice.agent_runner import GoalPose

        pose = GoalPose()
        pose.record_motion(
            "turn",
            {"degrees": 30},
            {"ok": False, "error": "turn_stalled", "measured_degrees": 12.0},
        )
        self.assertEqual(pose.heading, 12.0)
        self.assertEqual(pose.recent_actions, ["turned 30 left (measured 12)"])

    def test_failing_camera_still_returns_motion_result_with_surroundings(self):
        steps = {"n": 0}

        def responder(_input_items):
            steps["n"] += 1
            if steps["n"] >= 2:
                return final("Done.")
            return call("move", arguments={"distance_meters": 0.5})

        def camera():
            raise RuntimeError("camera dead")

        openai = FakeOpenAI(responder)
        run(
            run_agent_goal(
                goal="Move forward.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=lambda _name, **_: {"ok": True, "result": "completed", "traveled_m": 0.5},
                robot_inspection_caller=lambda: self.TELEMETRY_SNAPSHOT,
                camera_snapshot_caller=camera,
            )
        )

        follow_up = openai.responses.calls[1]["input"]
        self.assertEqual(len(follow_up), 2)
        tool_output = json.loads(follow_up[0]["output"])
        self.assertTrue(tool_output["ok"])
        self.assertIn("surroundings", tool_output)
        self.assertIn("Position:", follow_up[1]["content"])

    def test_encoder_stall_gets_hint_action_log_and_caption(self):
        from voice.agent_runner import GoalPose, _action_log_line
        from voice.tools import encoder_stall_hint, motion_camera_caption

        line = _action_log_line(
            "move",
            {"distance_meters": 0.5},
            {"ok": False, "error": "encoder_no_progress", "traveled_m": 0.10},
        )
        self.assertEqual(line, "moved 0.5 forward (stalled at 0.10)")
        caption = motion_camera_caption("move", {"distance_meters": 0.5}, {"error": "encoder_no_progress", "traveled_m": 0.10})
        self.assertTrue(caption.endswith("(stalled)."))

        steps = {"n": 0}

        def responder(_input_items):
            steps["n"] += 1
            if steps["n"] >= 2:
                return final("Freed.")
            return call("move", arguments={"distance_meters": 0.5})

        def move(_name, **_):
            return {"ok": False, "error": "encoder_no_progress", "traveled_m": 0.10}

        openai = FakeOpenAI(responder)
        run(
            run_agent_goal(
                goal="Move forward.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=move,
            )
        )

        tool_output = json.loads(openai.responses.calls[1]["input"][0]["output"])
        self.assertEqual(tool_output["hint"], encoder_stall_hint())
        pose = GoalPose()
        pose.record_motion("move", {"distance_meters": 0.5}, tool_output)
        self.assertIn("stalled at 0.10", pose.recent_actions[0])

    def test_backward_encoder_stall_hint_advises_forward(self):
        from voice.tools import encoder_stall_hint

        hint = encoder_stall_hint(-0.3)
        self.assertIn("Drive forward straight", hint)
        self.assertNotIn("Back up straight", hint)

        steps = {"n": 0}

        def responder(_input_items):
            steps["n"] += 1
            if steps["n"] >= 2:
                return final("Freed.")
            return call("move", arguments={"distance_meters": -0.3})

        openai = FakeOpenAI(responder)
        run(
            run_agent_goal(
                goal="Back up.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=lambda _name, **_: {
                    "ok": False,
                    "error": "encoder_no_progress",
                    "traveled_m": -0.08,
                },
            )
        )

        tool_output = json.loads(openai.responses.calls[1]["input"][0]["output"])
        self.assertIn("Drive forward straight", tool_output["hint"])


    def test_forward_sensor_sentence_in_motion_caption(self):
        telemetry = {
            "sources": {"sensors": {"stale": False}},
            "sensors": {
                "status": "polling",
                "readings": [
                    {
                        "name": "front_left",
                        "distance_mm": 420,
                        "ok": True,
                        "role": "forward",
                        "stop_below_mm": 150,
                    },
                    {
                        "name": "front_center",
                        "distance_mm": 900,
                        "ok": True,
                        "role": "forward",
                        "stop_below_mm": 150,
                    },
                    {"name": "front_right", "ok": False, "role": "forward", "stop_below_mm": 150},
                ],
            },
        }

        steps = {"n": 0}

        def responder(_input_items):
            steps["n"] += 1
            if steps["n"] >= 2:
                return final("Done.")
            return call("move", arguments={"distance_meters": 0.5})

        openai = FakeOpenAI(responder)
        run(
            run_agent_goal(
                goal="Move forward.",
                stop_event=asyncio.Event(),
                openai_client=openai,
                openai_model="test-model",
                voice_state=VoiceState("voice"),
                motion_intent_caller=lambda _name, **_: {"ok": True, "result": "completed", "traveled_m": 0.5},
                robot_inspection_caller=lambda: telemetry,
                camera_snapshot_caller=lambda: b"jpeg-bytes",
            )
        )

        image_message = next(
            item for item in openai.responses.calls[1]["input"] if isinstance(item.get("content"), list)
        )
        caption = next(part["text"] for part in image_message["content"] if part.get("type") == "input_text")
        self.assertIn("Forward sensors: left 0.42 meters, center 0.90 meters, right unavailable.", caption)


if __name__ == "__main__":
    unittest.main()
