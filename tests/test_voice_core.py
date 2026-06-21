import asyncio
import json
import os
import sys
import unittest
from contextlib import suppress
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from voice.assistant import (
    CHECK_HEALTH_TOOL_NAME,
    CHECK_SURROUNDINGS_TOOL_NAME,
    END_SESSION_TOOL_NAME,
    FACE_ME_TOOL_NAME,
    LOOK_TOOL_NAME,
    MOVE_TOOL_NAME,
    START_GOAL_TOOL_NAME,
    ActiveGoal,
    ActiveTurn,
    AgentGoalRequest,
    AudioLevels,
    TurnRuntimeState,
    VoiceState,
    decide_barge_in_during_playback,
    handle_scribe_events,
    inspect_robot_snapshot,
    note_recent_barge_in_audio,
    note_utterance_barge_in_audio,
    note_mic_chunk,
    refresh_barge_in_gate,
    reset_recent_barge_in_audio,
    reset_utterance_barge_in_audio,
    run_assistant_turn,
    stream_openai_words,
)
from voice.tools import (
    INSPECT_SPEAKER_DIRECTION_TOOL_NAME,
    RobotToolCall,
    VoiceToolContext,
    agent_observation,
    dispatch_tool,
    parse_tool_arguments,
)
from voice.conversation import ConversationHistory
from config.voice import VoiceConfig
from voice.turn_policy import TurnPolicy, should_accept_barge_in, should_speculate, transcript_matches, turn_policy_from_config
from voice.assistant import update_near_end_gate


async def idle_forever():
    await asyncio.Event().wait()


class ConversationHistoryTest(unittest.TestCase):
    def test_first_request_contains_system_prompt_and_current_user(self):
        history = ConversationHistory()

        self.assertEqual(
            history.input_for("Hello", "System prompt"),
            [
                {"role": "system", "content": "System prompt"},
                {"role": "user", "content": "Hello"},
            ],
        )

    def test_history_keeps_newest_exchanges(self):
        history = ConversationHistory(max_exchanges=2)

        history.append_exchange("one", "first")
        history.append_exchange("two", "second")
        history.append_exchange("three", "third")

        self.assertEqual([exchange.user_text for exchange in history.exchanges()], ["two", "three"])


class TurnPolicyTest(unittest.TestCase):
    def test_complete_stable_partial_can_start_speculation(self):
        self.assertTrue(should_speculate("What is your name?"))

    def test_incomplete_partial_does_not_start_speculation(self):
        for ending in ["-", ",", ":", ";"]:
            self.assertFalse(should_speculate(f"What is your name{ending}"))

    def test_matching_commit_confirms_speculative_prompt(self):
        self.assertTrue(transcript_matches("What is your name?", "what is your name"))

    def test_interrupt_only_commit_does_not_start_turn(self):
        should_start, reason = TurnPolicy().commit_decision("Stop.")

        self.assertFalse(should_start)
        self.assertEqual(reason, "interrupt_only")

    def test_explicit_interrupt_words_can_barge_in_with_local_speech(self):
        self.assertTrue(
            should_accept_barge_in(
                "stop",
                assistant_speaking=True,
                gate_open=False,
                mic_rms=900,
            )
        )

    def test_explicit_interrupt_uses_user_active_threshold(self):
        should_barge_in, reason = TurnPolicy().barge_in_decision(
            "Okay stop please",
            assistant_speaking=True,
            gate_open=False,
            mic_rms=200,
            assistant_speech_elapsed_secs=1.0,
        )

        self.assertTrue(should_barge_in)
        self.assertEqual(reason, "explicit_interrupt")

    def test_explicit_interrupt_still_requires_local_speech(self):
        should_barge_in, reason = TurnPolicy().barge_in_decision(
            "stop",
            assistant_speaking=True,
            gate_open=False,
            mic_rms=12,
            assistant_speech_elapsed_secs=1.0,
        )

        self.assertFalse(should_barge_in)
        self.assertEqual(reason, "low_rms")

    def test_assistant_echo_partial_while_speaking_does_not_barge_in(self):
        should_barge_in, reason = TurnPolicy().barge_in_decision(
            "Sure. Here's a tiny one.",
            assistant_speaking=True,
            gate_open=True,
            assistant_speech_elapsed_secs=1.0,
            mic_rms=800,
            assistant_text="Sure, here's a tiny one: a small light can matter a lot.",
        )

        self.assertFalse(should_barge_in)
        self.assertEqual(reason, "assistant_echo")

    def test_turn_policy_from_config_disables_barge_in(self):
        policy = turn_policy_from_config(VoiceConfig(barge_in_enabled=False))
        should_barge_in, reason = policy.barge_in_decision(
            "please stop talking now",
            assistant_speaking=True,
            gate_open=True,
            mic_rms=1200,
            assistant_speech_elapsed_secs=1.0,
        )
        self.assertFalse(should_barge_in)
        self.assertEqual(reason, "disabled")

    def test_barge_in_threshold_uses_min_rms(self):
        should_barge_in, reason = TurnPolicy(barge_in_min_rms=500).barge_in_decision(
            "tell me another story please",
            assistant_speaking=True,
            gate_open=True,
            mic_rms=400,
            assistant_speech_elapsed_secs=1.0,
        )

        self.assertFalse(should_barge_in)
        self.assertEqual(reason, "low_rms")

    def test_sustained_near_end_gate_requires_continuous_audio(self):
        policy = TurnPolicy(barge_in_min_rms=500, barge_in_sustain_ms=350)
        above_since, gate_open, threshold, reason = update_near_end_gate(policy, None, 0.0, 900)
        self.assertFalse(gate_open)
        self.assertEqual(reason, "not_sustained")
        self.assertEqual(threshold, 500)
        above_since, gate_open, threshold, reason = update_near_end_gate(policy, above_since, 0.4, 900)
        self.assertTrue(gate_open)

    def test_scribe_upload_gate_holds_open_after_last_above_threshold(self):
        from voice.elevenlabs_io import MIC_SCRIBE_GATE_HOLD_SECS, update_scribe_upload_gate

        gate_open, last_above = update_scribe_upload_gate(0.0, 150, None)
        self.assertTrue(gate_open)
        self.assertEqual(last_above, 0.0)

        gate_open, last_above = update_scribe_upload_gate(0.1, 50, last_above)
        self.assertTrue(gate_open)
        self.assertEqual(last_above, 0.0)

        gate_open, last_above = update_scribe_upload_gate(0.0 + MIC_SCRIBE_GATE_HOLD_SECS + 0.01, 50, last_above)
        self.assertFalse(gate_open)
        self.assertIsNone(last_above)

    def test_scribe_upload_gate_stays_closed_below_threshold(self):
        from voice.elevenlabs_io import update_scribe_upload_gate

        gate_open, last_above = update_scribe_upload_gate(0.0, 50, None)
        self.assertFalse(gate_open)
        self.assertIsNone(last_above)

    def test_note_mic_chunk_tracks_peak(self):
        levels = AudioLevels()
        note_mic_chunk(levels, 120)
        note_mic_chunk(levels, 450)
        note_mic_chunk(levels, 200)
        self.assertEqual(levels.mic_peak, 450)
        self.assertEqual(levels.mic_last, 200)

    def test_refresh_barge_in_gate_writes_threshold_and_gate(self):
        policy = TurnPolicy(barge_in_min_rms=500)
        levels = AudioLevels()
        _, gate_open, threshold, reason = refresh_barge_in_gate(levels, 0.0, policy, False, 100)
        self.assertFalse(gate_open)
        self.assertEqual(threshold, 500)
        self.assertEqual(reason, "assistant_not_speaking")
        self.assertEqual(levels.threshold_rms, 500)

    def test_single_loud_spike_does_not_open_gate_without_sustain(self):
        policy = TurnPolicy(barge_in_min_rms=500, barge_in_sustain_ms=350)
        _, gate_open, _, reason = update_near_end_gate(policy, None, 1.0, 1200)
        self.assertFalse(gate_open)
        self.assertEqual(reason, "not_sustained")

    def test_substantial_partial_requires_open_gate(self):
        should_barge_in, reason = TurnPolicy().barge_in_decision(
            "tell me another story please",
            assistant_speaking=True,
            gate_open=False,
            mic_rms=900,
            assistant_speech_elapsed_secs=1.0,
            gate_reason="not_sustained",
        )
        self.assertFalse(should_barge_in)
        self.assertEqual(reason, "not_sustained")


class DecideBargeInDuringPlaybackTest(unittest.TestCase):
    def _fake_turn(self, *, streamed: str = "", speech_started_at: float = 0.0):
        return SimpleNamespace(
            assistant_streamed_text=lambda: streamed,
            speech_elapsed_secs=lambda now: now - speech_started_at,
        )

    def test_uses_recent_barge_in_inputs_within_window(self):
        policy = TurnPolicy(barge_in_min_rms=500, local_speech_window_secs=1.0)
        state = TurnRuntimeState(
            last_local_speech_rms=100,
            gate_open=False,
            gate_last_reason="low_rms",
            recent_barge_in_mic_rms=1200,
            recent_barge_in_gate_open=True,
            recent_barge_in_gate_reason="substantial_partial",
            recent_barge_in_audio_at=0.5,
        )
        levels = AudioLevels(playback_rms=400, playback_at=1.0)

        outcome = decide_barge_in_during_playback(
            "tell me another story please",
            now=1.0,
            active_turn=self._fake_turn(),
            state=state,
            levels=levels,
            policy=policy,
        )

        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.reason, "substantial_partial")
        self.assertEqual(outcome.mic_rms, 1200)
        self.assertTrue(outcome.gate_open)
        self.assertEqual(outcome.playback_rms, 400)

    def test_falls_back_to_current_inputs_after_window_expires(self):
        policy = TurnPolicy(barge_in_min_rms=500, local_speech_window_secs=0.5)
        state = TurnRuntimeState(
            last_local_speech_rms=100,
            gate_open=False,
            gate_last_reason="low_rms",
            recent_barge_in_mic_rms=1200,
            recent_barge_in_gate_open=True,
            recent_barge_in_gate_reason="substantial_partial",
            recent_barge_in_audio_at=0.0,
        )

        outcome = decide_barge_in_during_playback(
            "tell me another story please",
            now=10.0,
            active_turn=self._fake_turn(),
            state=state,
            levels=AudioLevels(),
            policy=policy,
        )

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "low_rms")
        self.assertEqual(outcome.mic_rms, 100)
        self.assertFalse(outcome.gate_open)

    def test_explicit_interrupt_accepted_even_with_closed_gate(self):
        policy = TurnPolicy(barge_in_min_rms=700, local_speech_window_secs=0.5)
        state = TurnRuntimeState(
            last_local_speech_rms=200,
            gate_open=False,
            gate_last_reason="low_rms",
            recent_barge_in_audio_at=0.0,
        )

        outcome = decide_barge_in_during_playback(
            "wait",
            now=10.0,
            active_turn=self._fake_turn(),
            state=state,
            levels=AudioLevels(),
            policy=policy,
        )

        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.reason, "explicit_interrupt")

    def test_assistant_echo_is_rejected(self):
        policy = TurnPolicy(barge_in_min_rms=500)
        state = TurnRuntimeState(
            last_local_speech_rms=900,
            gate_open=True,
            gate_last_reason="substantial_partial",
        )

        outcome = decide_barge_in_during_playback(
            "Sure, here's a tiny one.",
            now=1.0,
            active_turn=self._fake_turn(streamed="Sure, here's a tiny one: a small light can matter a lot."),
            state=state,
            levels=AudioLevels(),
            policy=policy,
        )

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "assistant_echo")


class BargeInAudioMemoryTest(unittest.TestCase):
    def test_reset_recent_barge_in_audio_clears_recent_fields(self):
        state = TurnRuntimeState(
            recent_barge_in_mic_rms=900,
            recent_barge_in_gate_open=True,
            recent_barge_in_gate_reason="substantial_partial",
            recent_barge_in_audio_at=3.0,
        )

        reset_recent_barge_in_audio(state)

        self.assertEqual(state.recent_barge_in_mic_rms, 0)
        self.assertFalse(state.recent_barge_in_gate_open)
        self.assertEqual(state.recent_barge_in_gate_reason, "assistant_not_speaking")
        self.assertEqual(state.recent_barge_in_audio_at, 0.0)

    def test_reset_utterance_barge_in_audio_clears_utterance_fields(self):
        state = TurnRuntimeState(
            utterance_barge_in_mic_rms=900,
            utterance_barge_in_gate_open=True,
            utterance_barge_in_gate_reason="substantial_partial",
            utterance_barge_in_audio_at=3.0,
        )

        reset_utterance_barge_in_audio(state)

        self.assertEqual(state.utterance_barge_in_mic_rms, 0)
        self.assertFalse(state.utterance_barge_in_gate_open)
        self.assertEqual(state.utterance_barge_in_gate_reason, "assistant_not_speaking")
        self.assertEqual(state.utterance_barge_in_audio_at, 0.0)

    def test_note_utterance_barge_in_audio_keeps_peak_rms_and_open_gate(self):
        state = TurnRuntimeState(
            last_local_speech_rms=700,
            gate_open=True,
            gate_last_reason="substantial_partial",
        )

        note_utterance_barge_in_audio(state, now=1.0)
        state.last_local_speech_rms = 500
        state.gate_open = False
        state.gate_last_reason = "low_rms"
        note_utterance_barge_in_audio(state, now=1.2)

        self.assertEqual(state.utterance_barge_in_mic_rms, 700)
        self.assertTrue(state.utterance_barge_in_gate_open)
        self.assertEqual(state.utterance_barge_in_gate_reason, "low_rms")
        self.assertEqual(state.utterance_barge_in_audio_at, 1.2)

    def test_note_recent_barge_in_audio_keeps_peak_inside_window(self):
        state = TurnRuntimeState(
            last_local_speech_rms=500,
            gate_open=False,
            gate_last_reason="low_rms",
            recent_barge_in_audio_at=1.0,
            recent_barge_in_mic_rms=800,
            recent_barge_in_gate_open=True,
            recent_barge_in_gate_reason="substantial_partial",
        )
        policy = TurnPolicy(local_speech_window_secs=1.0)

        note_recent_barge_in_audio(state, now=1.5, policy=policy)

        self.assertEqual(state.recent_barge_in_mic_rms, 800)
        self.assertTrue(state.recent_barge_in_gate_open)
        self.assertEqual(state.recent_barge_in_gate_reason, "low_rms")
        self.assertEqual(state.recent_barge_in_audio_at, 1.5)

    def test_note_recent_barge_in_audio_resets_gate_when_window_is_stale(self):
        state = TurnRuntimeState(
            last_local_speech_rms=500,
            gate_open=False,
            gate_last_reason="low_rms",
            recent_barge_in_audio_at=1.0,
            recent_barge_in_mic_rms=800,
            recent_barge_in_gate_open=True,
            recent_barge_in_gate_reason="substantial_partial",
        )
        policy = TurnPolicy(local_speech_window_secs=0.5)

        note_recent_barge_in_audio(state, now=2.0, policy=policy)

        self.assertEqual(state.recent_barge_in_mic_rms, 500)
        self.assertFalse(state.recent_barge_in_gate_open)
        self.assertEqual(state.recent_barge_in_gate_reason, "low_rms")
        self.assertEqual(state.recent_barge_in_audio_at, 2.0)


class ActiveTurnTest(unittest.TestCase):
    def test_cancelling_turn_cancels_assistant_task(self):
        async def run():
            task = asyncio.create_task(idle_forever())
            turn = ActiveTurn(turn_id=1, prompt="hello", speculative=False, task=task)

            await turn.cancel("test")

            self.assertTrue(task.cancelled())
            self.assertFalse(turn.is_active())

        asyncio.run(run())

    def test_playback_is_gated_for_speculative_turns_until_confirmation(self):
        async def run():
            task = asyncio.create_task(idle_forever())
            turn = ActiveTurn(turn_id=1, prompt="hello there", speculative=True, task=task)

            self.assertFalse(turn.playback_event.is_set())

            await turn.confirm("hello there")

            self.assertTrue(turn.playback_event.is_set())
            self.assertFalse(turn.speculative)

            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        asyncio.run(run())


class AssistantStreamingTest(unittest.TestCase):
    def test_inspect_robot_snapshot_returns_only_live_curated_status(self):
        result = inspect_robot_snapshot(
            {
                "sources": {
                    "gamepad_teleop": {"stale": False},
                    "pi_battery": {"stale": False},
                    "motor_rail": {"stale": False},
                    "sensors": {"stale": False},
                    "vision": {"stale": True},
                    "system": {"stale": False},
                },
                "motor_battery": {
                    "status": "ok",
                    "pack_voltage": 11.8,
                    "cell_voltage": 3.93,
                    "percent_estimate": 67,
                    "chemistry": "lipo",
                    "cell_count": 3,
                    "capacity_mah": 2200,
                },
                "pi_battery": {
                    "status": "low",
                    "pack_voltage": 13.3,
                    "percent": 25,
                    "current_amps": -0.4,
                    "power_state": "discharging",
                    "runtime_minutes": 120,
                    "warning_voltage": 13.3,
                    "shutdown_voltage": 13.0,
                    "shutdown_pending": False,
                },
                "drive_status": {
                    "state": "stopped",
                    "stop_reason": "idle",
                    "safety_blocked": False,
                    "safety_reason": None,
                    "roboclaw_ready": True,
                    "telemetry_publish_failures": 99,
                },
                "motor_rail": {"state": "on", "reason": "motion_power_requested", "last_pack_voltage": 11.8},
                "sensors": {
                    "status": "polling",
                    "readings": [
                        {"name": "front_center", "distance_mm": 420, "ok": True, "kind": "vl53l1x"},
                    ],
                },
                "vision": {"status": "detecting", "faces": [{}, {}]},
                "pi": {
                    "uptime_seconds": 100,
                    "load_1m": 0.2,
                    "soc_temp_c": 48.0,
                    "disk_used_percent": 25.0,
                    "throttled_flags": "0x0",
                    "memory_used_mb": 512,
                },
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["battery"]["status"], "ok")
        self.assertEqual(result["battery"]["percent_estimate"], 67)
        self.assertEqual(result["battery"]["capacity_mah"], 2200)
        self.assertEqual(result["pi_battery"]["status"], "low")
        self.assertEqual(result["pi_battery"]["pack_voltage"], 13.3)
        self.assertEqual(result["pi_battery"]["shutdown_voltage"], 13.0)
        self.assertFalse(result["pi_battery"]["shutdown_pending"])
        self.assertEqual(result["drive"]["state"], "stopped")
        self.assertNotIn("telemetry_publish_failures", result["drive"])
        self.assertEqual(
            result["sensors"]["readings"],
            [{"name": "front_center", "distance_mm": 420, "ok": True}],
        )
        self.assertEqual(result["vision"], {"available": False})
        self.assertNotIn("memory_used_mb", result["pi"])

    def test_inspect_robot_snapshot_reads_battery_and_drive_from_robot_motion(self):
        result = inspect_robot_snapshot(
            {
                "sources": {
                    "robot_motion": {"stale": False},
                    "gamepad_teleop": {"stale": True},
                },
                "motor_battery": {
                    "status": "ok",
                    "pack_voltage": 11.6,
                    "cell_voltage": 3.87,
                    "percent_estimate": 53,
                    "chemistry": "lipo",
                    "cell_count": 3,
                    "capacity_mah": 2200,
                },
                "drive_status": {"state": "stopped", "roboclaw_ready": True},
            }
        )

        self.assertTrue(result["battery"]["available"])
        self.assertEqual(result["battery"]["pack_voltage"], 11.6)
        self.assertEqual(result["battery"]["percent_estimate"], 53)
        self.assertTrue(result["drive"]["available"])
        self.assertEqual(result["drive"]["state"], "stopped")

    def test_inspect_robot_snapshot_exposes_cached_stale_motor_battery(self):
        result = inspect_robot_snapshot(
            {
                "sources": {"robot_motion": {"stale": False}},
                "motor_battery": {
                    "status": "ok",
                    "pack_voltage": 11.7,
                    "cell_voltage": 3.9,
                    "percent_estimate": 60,
                    "chemistry": "lipo",
                    "cell_count": 3,
                    "capacity_mah": 2200,
                    "stale": True,
                    "stale_reason": "idle_no_gamepad",
                    "cached_at": 1234.0,
                },
            }
        )

        self.assertTrue(result["battery"]["available"])
        self.assertTrue(result["battery"]["stale"])
        self.assertEqual(result["battery"]["stale_reason"], "idle_no_gamepad")
        self.assertEqual(result["battery"]["cached_at"], 1234.0)

    def test_inspect_robot_snapshot_reads_live_pi_battery(self):
        result = inspect_robot_snapshot(
            {
                "sources": {"pi_battery": {"stale": False}},
                "pi_battery": {
                    "status": "critical",
                    "pack_voltage": 13.0,
                    "percent": 12,
                    "current_amps": -0.8,
                    "power_state": "discharging",
                    "runtime_minutes": 8,
                    "warning_voltage": 13.3,
                    "shutdown_voltage": 13.0,
                    "shutdown_pending": True,
                    "cell_voltages": [3.25, 3.25, 3.25, 3.25],
                    "usb_c_voltage": 0.0,
                },
            }
        )

        self.assertTrue(result["pi_battery"]["available"])
        self.assertEqual(result["pi_battery"]["status"], "critical")
        self.assertEqual(result["pi_battery"]["pack_voltage"], 13.0)
        self.assertTrue(result["pi_battery"]["shutdown_pending"])
        self.assertNotIn("cell_voltages", result["pi_battery"])
        self.assertNotIn("usb_c_voltage", result["pi_battery"])

    def test_inspect_robot_snapshot_hides_stale_pi_battery(self):
        result = inspect_robot_snapshot(
            {
                "sources": {"pi_battery": {"stale": True}},
                "pi_battery": {"status": "critical", "pack_voltage": 13.0},
            }
        )

        self.assertFalse(result["pi_battery"]["available"])

    def test_inspect_robot_snapshot_falls_back_to_gamepad_teleop_battery_and_drive(self):
        result = inspect_robot_snapshot(
            {
                "sources": {"gamepad_teleop": {"stale": False}},
                "motor_battery": {"status": "ok", "pack_voltage": 11.9},
                "drive_status": {"state": "driving"},
            }
        )

        self.assertTrue(result["battery"]["available"])
        self.assertTrue(result["drive"]["available"])

    def test_inspect_robot_snapshot_does_not_fall_back_after_motion_stales(self):
        result = inspect_robot_snapshot(
            {
                "sources": {
                    "robot_motion": {"stale": True, "last_seen": 100.0},
                    "gamepad_teleop": {"stale": False},
                },
                "motor_battery": {"status": "ok", "pack_voltage": 11.9},
                "drive_status": {"state": "driving"},
            }
        )

        self.assertFalse(result["battery"]["available"])
        self.assertFalse(result["drive"]["available"])

    def test_stream_openai_words_feeds_robot_inspection_back_to_model(self):
        async def run():
            class FakeResponses:
                def __init__(self):
                    self.calls = []

                async def create(self, **kwargs):
                    self.calls.append(kwargs)
                    if len(self.calls) == 1:
                        events = [
                            SimpleNamespace(
                                type="response.output_item.done",
                                item=SimpleNamespace(
                                    type="function_call",
                                    name=CHECK_HEALTH_TOOL_NAME,
                                    call_id="call_inspect",
                                ),
                            ),
                            SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_1")),
                        ]
                    else:
                        events = [
                            SimpleNamespace(type="response.output_text.delta", delta="I feel fine."),
                            SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_2")),
                        ]

                    async def stream():
                        for event in events:
                            yield event

                    return stream()

            fake_responses = FakeResponses()
            chunks = [
                chunk
                async for chunk in stream_openai_words(
                    [{"role": "user", "content": "How are you feeling?"}],
                    SimpleNamespace(responses=fake_responses),
                    VoiceState("test-voice"),
                    robot_inspection_caller=lambda: {
                        "sources": {"gamepad_teleop": {"stale": False}},
                        "motor_battery": {"status": "ok", "pack_voltage": 11.8, "cell_voltage": 3.93},
                    },
                )
            ]

            self.assertEqual(chunks, ["I feel fine."])
            tool_output = json.loads(fake_responses.calls[1]["input"][0]["output"])
            self.assertEqual(tool_output["battery"]["status"], "ok")
            self.assertNotIn("sensors", tool_output)

        asyncio.run(run())

    def test_stream_openai_words_suppresses_text_from_tool_calling_response(self):
        async def run():
            class FakeResponses:
                def __init__(self):
                    self.calls = []

                async def create(self, **kwargs):
                    self.calls.append(kwargs)
                    if len(self.calls) == 1:
                        events = [
                            SimpleNamespace(type="response.output_text.delta", delta="Let me check."),
                            SimpleNamespace(
                                type="response.output_item.done",
                                item=SimpleNamespace(
                                    type="function_call",
                                    name=LOOK_TOOL_NAME,
                                    call_id="call_look",
                                ),
                            ),
                            SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_1")),
                        ]
                    else:
                        events = [
                            SimpleNamespace(type="response.output_text.delta", delta="I see you."),
                            SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_2")),
                        ]

                    async def stream():
                        for event in events:
                            yield event

                    return stream()

            fake_responses = FakeResponses()
            chunks = [
                chunk
                async for chunk in stream_openai_words(
                    [{"role": "user", "content": "What do you see?"}],
                    SimpleNamespace(responses=fake_responses),
                    VoiceState("test-voice"),
                    camera_snapshot_caller=lambda: b"jpeg-bytes",
                )
            ]

            self.assertEqual(chunks, ["I see you."])

        asyncio.run(run())

    def test_stream_openai_words_invokes_face_me_caller(self):
        async def run():
            class FakeResponses:
                def __init__(self):
                    self.calls = []

                async def create(self, **kwargs):
                    self.calls.append(kwargs)
                    if len(self.calls) == 1:
                        events = [
                            SimpleNamespace(
                                type="response.output_item.done",
                                item=SimpleNamespace(
                                    type="function_call",
                                    name=FACE_ME_TOOL_NAME,
                                    call_id="call_face",
                                ),
                            ),
                            SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_1")),
                        ]
                    else:
                        events = [
                            SimpleNamespace(type="response.output_text.delta", delta="Turning to face you."),
                            SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_2")),
                        ]

                    async def stream():
                        for event in events:
                            yield event

                    return stream()

            fake_responses = FakeResponses()
            calls = []

            def face_me_caller():
                calls.append(True)
                return {"ok": True, "result": "completed", "relative_degrees": 90}

            chunks = [
                chunk
                async for chunk in stream_openai_words(
                    [{"role": "user", "content": "Face me."}],
                    SimpleNamespace(responses=fake_responses),
                    VoiceState("test-voice"),
                    face_me_caller=face_me_caller,
                )
            ]

            self.assertEqual("".join(chunks), "Turning to face you.")
            self.assertEqual(len(calls), 1)
            tool_output = json.loads(fake_responses.calls[1]["input"][0]["output"])
            self.assertEqual(tool_output, {"ok": True, "result": "completed", "relative_degrees": 90})

        asyncio.run(run())

    def test_stream_openai_words_face_me_unavailable_without_caller(self):
        async def run():
            class FakeResponses:
                def __init__(self):
                    self.calls = []

                async def create(self, **kwargs):
                    self.calls.append(kwargs)
                    if len(self.calls) == 1:
                        events = [
                            SimpleNamespace(
                                type="response.output_item.done",
                                item=SimpleNamespace(
                                    type="function_call",
                                    name=FACE_ME_TOOL_NAME,
                                    call_id="call_face",
                                ),
                            ),
                            SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_1")),
                        ]
                    else:
                        events = [
                            SimpleNamespace(type="response.output_text.delta", delta="Sorry."),
                            SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_2")),
                        ]

                    async def stream():
                        for event in events:
                            yield event

                    return stream()

            fake_responses = FakeResponses()
            async for _chunk in stream_openai_words(
                [{"role": "user", "content": "Face me."}],
                SimpleNamespace(responses=fake_responses),
                VoiceState("test-voice"),
            ):
                pass

            tool_output = json.loads(fake_responses.calls[1]["input"][0]["output"])
            self.assertEqual(tool_output, {"ok": False, "error": "face_me_unavailable"})

        asyncio.run(run())

    def test_stream_openai_words_start_goal_yields_goal_request(self):
        async def run():
            class FakeResponses:
                def __init__(self):
                    self.calls = []

                async def create(self, **kwargs):
                    self.calls.append(kwargs)
                    events = [
                        SimpleNamespace(
                            type="response.output_item.done",
                            item=SimpleNamespace(
                                type="function_call",
                                name=START_GOAL_TOOL_NAME,
                                call_id="call_goal",
                                arguments=json.dumps({"goal": "move toward me and stop when close"}),
                            ),
                        ),
                        SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_1")),
                    ]

                    async def stream():
                        for event in events:
                            yield event

                    return stream()

            fake_responses = FakeResponses()
            chunks = [
                chunk
                async for chunk in stream_openai_words(
                    [{"role": "user", "content": "Move toward me."}],
                    SimpleNamespace(responses=fake_responses),
                    VoiceState("test-voice"),
                )
            ]

            self.assertEqual(chunks, [AgentGoalRequest(goal="move toward me and stop when close")])
            self.assertEqual([chunk for chunk in chunks if isinstance(chunk, str)], [])
            # The goal handoff ends the turn without feeding a tool output back, so
            # the model is only ever called once.
            self.assertEqual(len(fake_responses.calls), 1)

        asyncio.run(run())

    def test_stream_openai_words_start_goal_carries_co_emitted_text_as_preamble(self):
        async def run():
            class FakeResponses:
                def __init__(self):
                    self.calls = []

                async def create(self, **kwargs):
                    self.calls.append(kwargs)
                    events = [
                        SimpleNamespace(type="response.output_text.delta", delta="On it."),
                        SimpleNamespace(
                            type="response.output_item.done",
                            item=SimpleNamespace(
                                type="function_call",
                                name=START_GOAL_TOOL_NAME,
                                call_id="call_goal",
                                arguments=json.dumps({"goal": "find the ball"}),
                            ),
                        ),
                        SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_1")),
                    ]

                    async def stream():
                        for event in events:
                            yield event

                    return stream()

            fake_responses = FakeResponses()
            chunks = [
                chunk
                async for chunk in stream_openai_words(
                    [{"role": "user", "content": "Find the ball."}],
                    SimpleNamespace(responses=fake_responses),
                    VoiceState("test-voice"),
                )
            ]

            # Text emitted alongside start_goal rides along as the goal's preamble,
            # so the handoff opens with a spoken acknowledgement.
            self.assertEqual(
                chunks, [AgentGoalRequest(goal="find the ball", preamble="On it.")]
            )
            self.assertEqual(len(fake_responses.calls), 1)

        asyncio.run(run())

    def test_stream_openai_words_retries_create_once(self):
        async def run():
            class FakeResponses:
                def __init__(self):
                    self.calls = 0

                async def create(self, **_kwargs):
                    self.calls += 1
                    if self.calls == 1:
                        raise RuntimeError("temporary openai failure")

                    async def stream():
                        yield SimpleNamespace(type="response.output_text.delta", delta="Hello there.")
                        yield SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_1"))

                    return stream()

            fake_responses = FakeResponses()

            with mock.patch("voice.assistant.OPENAI_CREATE_RETRY_DELAY_SECS", 0):
                chunks = [
                    chunk
                    async for chunk in stream_openai_words(
                        [{"role": "user", "content": "Hi"}],
                        SimpleNamespace(responses=fake_responses),
                        VoiceState("test-voice"),
                    )
                ]

            self.assertEqual(fake_responses.calls, 2)
            self.assertEqual(chunks, ["Hello there."])

        asyncio.run(run())

    def test_stream_openai_words_uses_configured_model_without_reasoning(self):
        async def run():
            calls = []

            class FakeResponses:
                async def create(self, **kwargs):
                    calls.append(kwargs)

                    async def stream():
                        yield SimpleNamespace(type="response.output_text.delta", delta="Hello.")
                        yield SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_1"))

                    return stream()

            chunks = [
                chunk
                async for chunk in stream_openai_words(
                    [{"role": "user", "content": "Hi"}],
                    SimpleNamespace(responses=FakeResponses()),
                    VoiceState("test-voice"),
                    openai_model="gpt-5.5",
                )
            ]

            self.assertEqual(chunks, ["Hello."])
            self.assertEqual(calls[0]["model"], "gpt-5.5")
            self.assertEqual(calls[0]["reasoning"], {"effort": "none"})

        asyncio.run(run())

    def test_stream_openai_words_allows_tool_first_end_session_goodbye(self):
        async def run():
            pending = [False]

            class FakeResponses:
                def __init__(self):
                    self.calls = []

                async def create(self, **kwargs):
                    self.calls.append(kwargs)
                    if len(self.calls) == 1:
                        events = [
                            SimpleNamespace(
                                type="response.output_item.done",
                                item=SimpleNamespace(
                                    type="function_call",
                                    name=END_SESSION_TOOL_NAME,
                                    call_id="call_end",
                                ),
                            ),
                            SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_1")),
                        ]
                    else:
                        events = [
                            SimpleNamespace(type="response.output_text.delta", delta="Bye."),
                            SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_2")),
                        ]

                    async def stream():
                        for event in events:
                            yield event

                    return stream()

            fake_responses = FakeResponses()

            chunks = [
                chunk
                async for chunk in stream_openai_words(
                    [{"role": "user", "content": "Goodbye"}],
                    SimpleNamespace(responses=fake_responses),
                    VoiceState("test-voice"),
                    end_session_pending=pending,
                )
            ]

            self.assertTrue(pending[0])
            self.assertEqual(chunks, ["Bye."])
            self.assertEqual(len(fake_responses.calls), 2)
            tool_output = json.loads(fake_responses.calls[1]["input"][0]["output"])
            self.assertEqual(tool_output, {"ok": True, "ended": True})

        asyncio.run(run())

    def test_run_assistant_turn_defers_session_end_until_after_tts(self):
        async def run():
            ended = []
            order = []

            class FakeResponses:
                def __init__(self):
                    self.calls = []

                async def create(self, **kwargs):
                    self.calls.append(kwargs)
                    if len(self.calls) == 1:
                        events = [
                            SimpleNamespace(type="response.output_text.delta", delta="Bye."),
                            SimpleNamespace(
                                type="response.output_item.done",
                                item=SimpleNamespace(
                                    type="function_call",
                                    name=END_SESSION_TOOL_NAME,
                                    call_id="call_end",
                                ),
                            ),
                            SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_1")),
                        ]
                    else:
                        events = [
                            SimpleNamespace(type="response.output_text.delta", delta="Bye."),
                            SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_2")),
                        ]

                    async def stream():
                        for event in events:
                            yield event

                    return stream()

            async def fake_speaker(text_chunks, *args, **kwargs):
                order.append("tts_start")
                async for chunk in text_chunks:
                    if isinstance(chunk, str):
                        order.append(f"tts:{chunk}")
                order.append("tts_done")

            fake_responses = FakeResponses()
            text = await run_assistant_turn(
                1,
                [{"role": "user", "content": "Goodbye"}],
                asyncio.Event(),
                asyncio.Event(),
                SimpleNamespace(responses=fake_responses),
                "key",
                VoiceState("test-voice"),
                tts_speaker=fake_speaker,
                session_end_caller=lambda: ended.append(True),
            )

            self.assertEqual(text, "Bye.")
            self.assertEqual(order, ["tts_start", "tts:Bye.", "tts_done"])
            self.assertEqual(ended, [True])
            self.assertEqual(len(fake_responses.calls), 2)

        asyncio.run(run())

    def test_run_assistant_turn_start_goal_skips_tts_speaker(self):
        async def run():
            class FakeResponses:
                def __init__(self):
                    self.calls = []

                async def create(self, **kwargs):
                    self.calls.append(kwargs)
                    events = [
                        SimpleNamespace(
                            type="response.output_item.done",
                            item=SimpleNamespace(
                                type="function_call",
                                name=START_GOAL_TOOL_NAME,
                                call_id="call_goal",
                                arguments=json.dumps({"goal": "patrol the room"}),
                            ),
                        ),
                        SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_1")),
                    ]

                    async def stream():
                        for event in events:
                            yield event

                    return stream()

            spoken = []

            async def fake_speaker(text_chunks, *args, **kwargs):
                spoken.append(True)
                async for _chunk in text_chunks:
                    pass

            result = await run_assistant_turn(
                1,
                [{"role": "user", "content": "Patrol the room."}],
                asyncio.Event(),
                asyncio.Event(),
                SimpleNamespace(responses=FakeResponses()),
                "key",
                VoiceState("test-voice"),
                tts_speaker=fake_speaker,
            )

            self.assertEqual(result, AgentGoalRequest(goal="patrol the room"))
            # A pure handoff never opens the TTS/playback path.
            self.assertEqual(spoken, [])

        asyncio.run(run())

    def test_stream_openai_words_stops_after_end_session_followup(self):
        async def run():
            pending = [False]

            class FakeResponses:
                def __init__(self):
                    self.calls = 0

                async def create(self, **kwargs):
                    self.calls += 1
                    events = [
                        SimpleNamespace(
                            type="response.output_item.done",
                            item=SimpleNamespace(
                                type="function_call",
                                name=END_SESSION_TOOL_NAME,
                                call_id="call_end",
                            ),
                        ),
                        SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_1")),
                    ]

                    async def stream():
                        for event in events:
                            yield event

                    return stream()

            fake = FakeResponses()
            async for _chunk in stream_openai_words(
                [{"role": "user", "content": "Goodbye"}],
                SimpleNamespace(responses=fake),
                VoiceState("test-voice"),
                end_session_pending=pending,
            ):
                pass

            self.assertTrue(pending[0])
            self.assertEqual(fake.calls, 2)

        asyncio.run(run())

    def test_stream_openai_words_attaches_camera_snapshot_for_look_tool(self):
        async def run():
            class FakeResponses:
                def __init__(self):
                    self.calls = []

                async def create(self, **kwargs):
                    self.calls.append(kwargs)
                    if len(self.calls) == 1:
                        events = [
                            SimpleNamespace(
                                type="response.output_item.done",
                                item=SimpleNamespace(
                                    type="function_call",
                                    name=LOOK_TOOL_NAME,
                                    call_id="call_look",
                                ),
                            ),
                            SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_1")),
                        ]
                    else:
                        events = [
                            SimpleNamespace(type="response.output_text.delta", delta="I see you."),
                            SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_2")),
                        ]

                    async def stream():
                        for event in events:
                            yield event

                    return stream()

            fake_responses = FakeResponses()

            chunks = [
                chunk
                async for chunk in stream_openai_words(
                    [{"role": "user", "content": "What do you see?"}],
                    SimpleNamespace(responses=fake_responses),
                    VoiceState("test-voice"),
                    camera_snapshot_caller=lambda: b"jpeg-bytes",
                )
            ]

            self.assertEqual(chunks, ["I see you."])
            next_input = fake_responses.calls[1]["input"]
            tool_output = json.loads(next_input[0]["output"])
            self.assertEqual(tool_output, {"ok": True, "image_attached": True})
            self.assertEqual(next_input[1]["role"], "user")
            self.assertEqual(next_input[1]["content"][0]["type"], "input_text")
            self.assertEqual(next_input[1]["content"][1]["type"], "input_image")
            self.assertTrue(next_input[1]["content"][1]["image_url"].startswith("data:image/jpeg;base64,"))

        asyncio.run(run())

    def test_completed_committed_turn_is_available_to_next_openai_request(self):
        async def run():
            started_inputs = []
            history = ConversationHistory()
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            policy = TurnPolicy(commit_playback_delay_secs=0.01)

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                started_inputs.append(openai_input)
                return f"assistant response {turn_id}"

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=policy,
                    conversation_history=history,
                    assistant_runner=fake_run_assistant_turn,
                )
            )

            await scribe_events.put({"type": "commit", "text": "What year is it?"})
            for _ in range(10):
                if history.exchanges():
                    break
                await asyncio.sleep(0.01)
            await scribe_events.put({"type": "commit", "text": "What month is it?"})
            await asyncio.sleep(0.05)

            self.assertEqual(
                started_inputs,
                [
                    [
                        {"role": "system", "content": "test system prompt"},
                        {"role": "user", "content": "What year is it?"},
                    ],
                    [
                        {"role": "system", "content": "test system prompt"},
                        {"role": "user", "content": "What year is it?"},
                        {"role": "assistant", "content": "assistant response 1"},
                        {"role": "user", "content": "What month is it?"},
                    ],
                ],
            )

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_goal_handoff_reaches_scribe_events(self):
        async def run():
            events = []
            history = ConversationHistory()
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                return AgentGoalRequest(goal="move toward me and stop when close")

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=TurnPolicy(),
                    conversation_history=history,
                    assistant_runner=fake_run_assistant_turn,
                    on_event=events.append,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Move toward me."})
            for _ in range(10):
                if any(event["type"] == "goal_handoff" for event in events):
                    break
                await asyncio.sleep(0.01)

            handoffs = [event for event in events if event["type"] == "goal_handoff"]
            self.assertEqual(len(handoffs), 1)
            self.assertEqual(handoffs[0]["goal"], "move toward me and stop when close")
            # The goal records its own result later, so nothing lands in history here.
            self.assertEqual(list(history.exchanges()), [])

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_goal_handoff_starts_runner_and_defers_history(self):
        async def run():
            events = []
            history = ConversationHistory()
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            goal_started = asyncio.Event()
            captured = {}

            async def fake_run_assistant_turn(
                turn_id, openai_input, playback_event, speaking_event,
                openai_client, elevenlabs_api_key, voice_state, on_assistant_chunk=None, **kwargs,
            ):
                return AgentGoalRequest(goal="patrol the room")

            async def fake_goal_runner(*, goal, stop_event, **kwargs):
                captured["goal"] = goal
                captured["stop_event"] = stop_event
                goal_started.set()
                await stop_event.wait()
                return ""

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=TurnPolicy(),
                    conversation_history=history,
                    assistant_runner=fake_run_assistant_turn,
                    goal_runner=fake_goal_runner,
                    on_event=events.append,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Patrol the room."})
            await asyncio.wait_for(goal_started.wait(), 1.0)

            self.assertEqual(captured["goal"], "patrol the room")
            self.assertTrue(any(event["type"] == "goal_start" for event in events))
            # The handoff turn must not commit history; only a terminal goal does.
            self.assertEqual(list(history.exchanges()), [])

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_goal_task_not_wrapped_by_assistant_turn_timeout(self):
        async def run():
            history = ConversationHistory()
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def fake_run_assistant_turn(
                turn_id, openai_input, playback_event, speaking_event,
                openai_client, elevenlabs_api_key, voice_state, on_assistant_chunk=None, **kwargs,
            ):
                return AgentGoalRequest(goal="long goal")

            async def fake_goal_runner(*, goal, stop_event, **kwargs):
                # Longer than the (patched) per-turn timeout: if the goal were wrapped
                # in asyncio.wait_for(ASSISTANT_TURN_TIMEOUT_SECS) it would be cancelled
                # and never commit history.
                await asyncio.sleep(0.06)
                return "Done patrolling."

            with mock.patch("voice.assistant.ASSISTANT_TURN_TIMEOUT_SECS", 0.01):
                handler_task = asyncio.create_task(
                    handle_scribe_events(
                        scribe_events,
                        openai_client=object(),
                        elevenlabs_api_key="test-key",
                        voice_state=VoiceState("test-voice"),
                        stop_event=stop_event,
                        system_prompt="test system prompt",
                        policy=TurnPolicy(),
                        conversation_history=history,
                        assistant_runner=fake_run_assistant_turn,
                        goal_runner=fake_goal_runner,
                    )
                )

                await scribe_events.put({"type": "commit", "text": "Patrol the room."})
                for _ in range(50):
                    if list(history.exchanges()):
                        break
                    await asyncio.sleep(0.01)

                exchanges = list(history.exchanges())
                self.assertEqual(len(exchanges), 1)
                self.assertEqual(exchanges[0].user_text, "Patrol the room.")
                self.assertEqual(exchanges[0].assistant_text, "Done patrolling.")

                stop_event.set()
                handler_task.cancel()
                with suppress(asyncio.CancelledError):
                    await handler_task

        asyncio.run(run())

    def test_committed_stop_cancels_goal_and_returns_to_listening(self):
        async def run():
            events = []
            statuses = []
            history = ConversationHistory()
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            goal_started = asyncio.Event()
            captured = {}

            async def fake_run_assistant_turn(
                turn_id, openai_input, playback_event, speaking_event,
                openai_client, elevenlabs_api_key, voice_state, on_assistant_chunk=None, **kwargs,
            ):
                return AgentGoalRequest(goal="follow me")

            async def fake_goal_runner(*, goal, stop_event, **kwargs):
                captured["stop_event"] = stop_event
                goal_started.set()
                await stop_event.wait()
                return "interrupted"

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=TurnPolicy(),
                    conversation_history=history,
                    assistant_runner=fake_run_assistant_turn,
                    goal_runner=fake_goal_runner,
                    on_event=events.append,
                    on_status=statuses.append,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Follow me around."})
            await asyncio.wait_for(goal_started.wait(), 1.0)

            await scribe_events.put({"type": "commit", "text": "Stop."})
            for _ in range(50):
                if any(event["type"] == "goal_cancel" for event in events):
                    break
                await asyncio.sleep(0.01)

            cancels = [event for event in events if event["type"] == "goal_cancel"]
            self.assertEqual(len(cancels), 1)
            self.assertTrue(captured["stop_event"].is_set())
            # An explicit interrupt returns to listening without committing history.
            self.assertEqual(list(history.exchanges()), [])
            last_status = [s for s in statuses if "status" in s][-1]
            self.assertEqual(last_status["status"], "listening")

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_committed_new_request_cancels_goal_and_starts_turn(self):
        async def run():
            events = []
            history = ConversationHistory()
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            goal_started = asyncio.Event()
            prompts = []

            async def fake_run_assistant_turn(
                turn_id, openai_input, playback_event, speaking_event,
                openai_client, elevenlabs_api_key, voice_state, on_assistant_chunk=None, **kwargs,
            ):
                prompt = openai_input[-1]["content"]
                prompts.append(prompt)
                if prompt == "Patrol the room.":
                    return AgentGoalRequest(goal="patrol the room")
                playback_event.set()
                return "The weather is fine."

            async def fake_goal_runner(*, goal, stop_event, **kwargs):
                goal_started.set()
                await stop_event.wait()
                return "interrupted"

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=TurnPolicy(),
                    conversation_history=history,
                    assistant_runner=fake_run_assistant_turn,
                    goal_runner=fake_goal_runner,
                    on_event=events.append,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Patrol the room."})
            await asyncio.wait_for(goal_started.wait(), 1.0)

            await scribe_events.put({"type": "commit", "text": "What is the weather?"})
            for _ in range(50):
                if list(history.exchanges()):
                    break
                await asyncio.sleep(0.01)

            self.assertTrue(any(event["type"] == "goal_cancel" for event in events))
            self.assertIn("What is the weather?", prompts)
            exchanges = list(history.exchanges())
            self.assertEqual(len(exchanges), 1)
            self.assertEqual(exchanges[0].user_text, "What is the weather?")
            self.assertEqual(exchanges[0].assistant_text, "The weather is fine.")

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_delayed_echo_of_progress_narration_is_suppressed_mid_goal(self):
        # A goal narrates a progress line, the narration finishes (so state.progress
        # clears) but the goal keeps working. A delayed STT echo of that line must be
        # recognized as assistant echo and must not cancel the goal or start a turn.
        async def run():
            events = []
            history = ConversationHistory()
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            narrated = asyncio.Event()
            prompts = []

            async def fake_run_assistant_turn(
                turn_id, openai_input, playback_event, speaking_event,
                openai_client, elevenlabs_api_key, voice_state, on_assistant_chunk=None, **kwargs,
            ):
                prompts.append(openai_input[-1]["content"])
                return AgentGoalRequest(goal="patrol the room")

            async def fake_progress_speaker(text_chunks, *args, **kwargs):
                async for _chunk in text_chunks:
                    pass

            async def fake_goal_runner(*, goal, stop_event, speak_progress=None, **kwargs):
                await speak_progress("Heading over to you now.")
                narrated.set()
                await stop_event.wait()
                return "I am right next to you."

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=TurnPolicy(),
                    conversation_history=history,
                    assistant_runner=fake_run_assistant_turn,
                    goal_runner=fake_goal_runner,
                    progress_speaker=fake_progress_speaker,
                    on_event=events.append,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Patrol the room."})
            await asyncio.wait_for(narrated.wait(), 1.0)

            await scribe_events.put({"type": "commit", "text": "Heading over to you now."})
            for _ in range(50):
                if any(event["type"] == "echo_suppressed" for event in events):
                    break
                await asyncio.sleep(0.01)

            # The echo was suppressed: the goal was never cancelled and no second
            # turn ran for the echoed line.
            self.assertTrue(any(event["type"] == "echo_suppressed" for event in events))
            self.assertFalse(any(event["type"] == "goal_cancel" for event in events))
            self.assertEqual(prompts, ["Patrol the room."])

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_shutdown_cancels_active_goal(self):
        async def run():
            history = ConversationHistory()
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            goal_started = asyncio.Event()
            captured = {}

            async def fake_run_assistant_turn(
                turn_id, openai_input, playback_event, speaking_event,
                openai_client, elevenlabs_api_key, voice_state, on_assistant_chunk=None, **kwargs,
            ):
                return AgentGoalRequest(goal="patrol the room")

            async def fake_goal_runner(*, goal, stop_event, **kwargs):
                captured["stop_event"] = stop_event
                goal_started.set()
                await stop_event.wait()
                return "interrupted"

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=TurnPolicy(),
                    conversation_history=history,
                    assistant_runner=fake_run_assistant_turn,
                    goal_runner=fake_goal_runner,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Patrol the room."})
            await asyncio.wait_for(goal_started.wait(), 1.0)

            # Shutdown tears the handler down; its cleanup must cancel the goal.
            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(handler_task, 1.0)

            self.assertTrue(captured["stop_event"].is_set())
            self.assertEqual(list(history.exchanges()), [])

        asyncio.run(run())

    def test_end_session_commit_cancels_active_goal(self):
        async def run():
            events = []
            ended = []
            history = ConversationHistory()
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            goal_started = asyncio.Event()
            captured = {}

            async def fake_run_assistant_turn(
                turn_id, openai_input, playback_event, speaking_event,
                openai_client, elevenlabs_api_key, voice_state, on_assistant_chunk=None, **kwargs,
            ):
                return AgentGoalRequest(goal="patrol the room")

            async def fake_goal_runner(*, goal, stop_event, **kwargs):
                captured["stop_event"] = stop_event
                goal_started.set()
                await stop_event.wait()
                return "interrupted"

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=TurnPolicy(),
                    conversation_history=history,
                    assistant_runner=fake_run_assistant_turn,
                    goal_runner=fake_goal_runner,
                    session_end_caller=lambda: ended.append(True),
                    on_event=events.append,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Patrol the room."})
            await asyncio.wait_for(goal_started.wait(), 1.0)

            await scribe_events.put({"type": "commit", "text": "Goodbye."})
            for _ in range(50):
                if any(event["type"] == "goal_cancel" for event in events):
                    break
                await asyncio.sleep(0.01)

            self.assertTrue(any(event["type"] == "goal_cancel" for event in events))
            self.assertTrue(captured["stop_event"].is_set())
            self.assertEqual(ended, [True])
            self.assertEqual(list(history.exchanges()), [])

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_goal_completion_appends_one_exchange(self):
        async def run():
            events = []
            history = ConversationHistory()
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def fake_run_assistant_turn(
                turn_id, openai_input, playback_event, speaking_event,
                openai_client, elevenlabs_api_key, voice_state, on_assistant_chunk=None, **kwargs,
            ):
                return AgentGoalRequest(goal="find the ball")

            async def fake_goal_runner(*, goal, stop_event, **kwargs):
                return "Found the ball by the couch."

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=TurnPolicy(),
                    conversation_history=history,
                    assistant_runner=fake_run_assistant_turn,
                    goal_runner=fake_goal_runner,
                    on_event=events.append,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Find the ball."})
            for _ in range(50):
                if list(history.exchanges()):
                    break
                await asyncio.sleep(0.01)

            exchanges = list(history.exchanges())
            self.assertEqual(len(exchanges), 1)
            self.assertEqual(exchanges[0].user_text, "Find the ball.")
            self.assertEqual(exchanges[0].assistant_text, "Found the ball by the couch.")
            done = [event for event in events if event["type"] == "goal_done"]
            self.assertEqual(len(done), 1)
            self.assertEqual(done[0]["text"], "Found the ball by the couch.")

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_committed_turn_starts_llm_before_playback_release(self):
        async def run():
            playback_opened = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            policy = TurnPolicy(commit_playback_delay_secs=0.04)

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                playback_opened.append(playback_event.is_set())
                await playback_event.wait()
                playback_opened.append(True)
                return "ok"

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=policy,
                    assistant_runner=fake_run_assistant_turn,
                )
            )

            await scribe_events.put({"type": "commit", "text": "What is your name?"})
            await asyncio.sleep(0.01)
            self.assertEqual(playback_opened, [False])

            await asyncio.sleep(0.05)
            self.assertEqual(playback_opened, [False, True])

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_low_user_active_rms_delays_committed_playback_release(self):
        async def run():
            playback_opened = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            policy = TurnPolicy(commit_playback_delay_secs=0.01, speculative_local_quiet_secs=0.04)

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                await playback_event.wait()
                playback_opened.append(True)
                return "ok"

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=policy,
                    assistant_runner=fake_run_assistant_turn,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Tell me a story"})
            await scribe_events.put({"type": "audio_activity", "rms": 120})
            for _ in range(5):
                await asyncio.sleep(0.01)
                await scribe_events.put({"type": "audio_activity", "rms": 120})

            self.assertEqual(playback_opened, [])

            await asyncio.sleep(0.06)
            self.assertEqual(playback_opened, [True])

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_continuation_commit_replaces_turn_before_playback_release(self):
        async def run():
            started_inputs = []
            cancelled = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            policy = TurnPolicy(commit_playback_delay_secs=0.08)

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                prompt = openai_input[-1]["content"]
                started_inputs.append(prompt)
                try:
                    await playback_event.wait()
                    return "ok"
                except asyncio.CancelledError:
                    cancelled.append(prompt)
                    raise

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=policy,
                    assistant_runner=fake_run_assistant_turn,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Tell me about batteries"})
            await asyncio.sleep(0.02)
            await scribe_events.put({"type": "commit", "text": "Tell me about batteries and motors"})
            await asyncio.sleep(0.02)

            self.assertEqual(started_inputs, ["Tell me about batteries", "Tell me about batteries and motors"])
            self.assertEqual(cancelled, ["Tell me about batteries"])

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_committed_assistant_echo_after_turn_does_not_start_next_turn(self):
        async def run():
            started_inputs = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                started_inputs.append(openai_input)
                if on_assistant_chunk:
                    on_assistant_chunk("Sure, here's a tiny one: a small light can matter a lot.")
                return "Sure, here's a tiny one: a small light can matter a lot."

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    assistant_runner=fake_run_assistant_turn,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Tell me a tiny story"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "commit", "text": "Sure, here's a tiny one."})
            await asyncio.sleep(0.05)

            self.assertEqual(len(started_inputs), 1)

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_recent_assistant_echo_partial_does_not_start_speculation(self):
        async def run():
            started_inputs = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            policy = TurnPolicy(speculative_partial_delay_secs=0.01)

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                started_inputs.append(openai_input)
                if on_assistant_chunk:
                    on_assistant_chunk("The answer is written on the blue card.")
                return "The answer is written on the blue card."

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=policy,
                    assistant_runner=fake_run_assistant_turn,
                )
            )

            await scribe_events.put({"type": "commit", "text": "What is the answer?"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "partial", "text": "The answer is written on the blue card."})
            await asyncio.sleep(0.05)

            self.assertEqual(len(started_inputs), 1)

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_stale_partial_matching_completed_turn_does_not_restart_it(self):
        async def run():
            started_inputs = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            policy = TurnPolicy(speculative_partial_delay_secs=0.01, speculative_local_quiet_secs=0)

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                started_inputs.append(openai_input[-1]["content"])
                return "ok"

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=policy,
                    assistant_runner=fake_run_assistant_turn,
                )
            )

            await scribe_events.put({"type": "partial", "text": "What do you see?"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "audio_activity", "rms": 120})
            await scribe_events.put({"type": "partial", "text": "What do you see?"})
            await asyncio.sleep(0.05)

            self.assertEqual(started_inputs, ["What do you see?"])

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_silent_audio_activity_does_not_delay_speculation(self):
        async def run():
            started_inputs = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            policy = TurnPolicy(speculative_partial_delay_secs=0.01, speculative_local_quiet_secs=0.04)

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                started_inputs.append(openai_input)
                return "ok"

            async def send_silence():
                while not stop_event.is_set():
                    await scribe_events.put({"type": "audio_activity", "rms": 0})
                    await asyncio.sleep(0.01)

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=policy,
                    assistant_runner=fake_run_assistant_turn,
                )
            )
            silence_task = asyncio.create_task(send_silence())

            await scribe_events.put({"type": "partial", "text": "What is your name?"})
            await asyncio.sleep(0.08)

            self.assertEqual(len(started_inputs), 1)

            stop_event.set()
            silence_task.cancel()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await silence_task
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_repeated_partial_without_new_audio_does_not_start_new_turn(self):
        async def run():
            started_inputs = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            policy = TurnPolicy(speculative_partial_delay_secs=0.01, speculative_local_quiet_secs=0)

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                started_inputs.append(openai_input)
                return "ok"

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=policy,
                    assistant_runner=fake_run_assistant_turn,
                )
            )

            await scribe_events.put({"type": "partial", "text": "Um, what do you see?"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "partial", "text": "Um, what do you see?"})
            await asyncio.sleep(0.05)

            self.assertEqual(len(started_inputs), 1)

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_user_speech_phase_tracks_partials_and_commits(self):
        async def run():
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            events: list[dict[str, object]] = []

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    on_event=events.append,
                    assistant_runner=idle_forever,
                )
            )

            await scribe_events.put({"type": "audio_activity", "rms": 200})
            await scribe_events.put({"type": "partial", "text": "hello"})
            await scribe_events.put({"type": "commit", "text": "hello there"})
            await asyncio.sleep(0.01)

            user_phases = [
                event
                for event in events
                if event.get("type") == "phase" and event.get("name") == "user_speech"
            ]
            self.assertEqual(
                [(event.get("on"),) for event in user_phases],
                [(True,), (False,)],
            )

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_quiet_audio_clears_uncommitted_user_speech_phase(self):
        async def run():
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            events: list[dict[str, object]] = []
            statuses: list[dict[str, object]] = []
            policy = TurnPolicy(local_speech_window_secs=0.02, speculative_partial_delay_secs=10.0)

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=policy,
                    on_event=events.append,
                    on_status=statuses.append,
                    assistant_runner=idle_forever,
                )
            )

            await scribe_events.put({"type": "audio_activity", "rms": 200})
            await scribe_events.put({"type": "partial", "text": "hello"})
            await asyncio.sleep(0.04)
            await scribe_events.put({"type": "audio_activity", "rms": 0})
            await asyncio.sleep(0.01)

            user_phases = [
                event
                for event in events
                if event.get("type") == "phase" and event.get("name") == "user_speech"
            ]
            hearing_phases = [
                event
                for event in events
                if event.get("type") == "phase" and event.get("name") == "hearing"
            ]
            self.assertEqual([(event.get("on"),) for event in user_phases], [(True,), (False,)])
            self.assertEqual([(event.get("on"),) for event in hearing_phases], [(True,), (False,)])
            self.assertTrue(any(status.get("status") == "listening" for status in statuses))

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_quiet_audio_clears_hearing_after_completed_turn(self):
        async def run():
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            events: list[dict[str, object]] = []
            policy = TurnPolicy(
                commit_playback_delay_secs=0.01,
                local_speech_window_secs=0.02,
                speculative_partial_delay_secs=10.0,
            )

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                return "Done."

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=policy,
                    on_event=events.append,
                    assistant_runner=fake_run_assistant_turn,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Move backward half a meter."})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "audio_activity", "rms": 200})
            await scribe_events.put({"type": "partial", "text": "stop"})
            await asyncio.sleep(0.04)
            await scribe_events.put({"type": "audio_activity", "rms": 0})
            await asyncio.sleep(0.01)

            hearing_phases = [
                event
                for event in events
                if event.get("type") == "phase" and event.get("name") == "hearing"
            ]
            self.assertEqual([(event.get("on"),) for event in hearing_phases[-2:]], [(True,), (False,)])

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_silent_completed_turn_returns_to_listening(self):
        async def run():
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            statuses: list[dict[str, object]] = []

            async def silent_assistant(*_args, **_kwargs):
                return ""

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    on_status=statuses.append,
                    assistant_runner=silent_assistant,
                    policy=TurnPolicy(commit_playback_delay_secs=10.0),
                )
            )

            await scribe_events.put({"type": "commit", "text": "please move forward"})
            for _ in range(100):
                if statuses and statuses[-1].get("status") == "listening":
                    break
                await asyncio.sleep(0.01)

            self.assertEqual(statuses[-1].get("status"), "listening")
            self.assertFalse(statuses[-1].get("assistant_speaking"))

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_end_session_commit_ends_session_without_model_turn(self):
        async def run():
            started_inputs = []
            ended = []
            statuses = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                started_inputs.append(openai_input)
                return "ok"

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    on_status=statuses.append,
                    assistant_runner=fake_run_assistant_turn,
                    session_end_caller=lambda: ended.append(True),
                )
            )

            await scribe_events.put({"type": "commit", "text": "End session"})
            await asyncio.sleep(0.02)

            self.assertEqual(started_inputs, [])
            self.assertEqual(ended, [True])
            self.assertEqual(statuses[-1]["last_committed_transcript"], "End session")

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_committed_transcript_during_tts_requires_open_gate(self):
        async def run():
            started_inputs = []
            cancelled = []
            statuses = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                started_inputs.append(openai_input)
                if on_assistant_chunk:
                    on_assistant_chunk("I am still talking.")
                playback_event.set()
                speaking_event.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(openai_input[-1]["content"])
                    raise

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    assistant_runner=fake_run_assistant_turn,
                    on_status=statuses.append,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Tell me something"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "commit", "text": "Tell me something else"})
            await asyncio.sleep(0.05)

            self.assertEqual(len(started_inputs), 1)
            self.assertEqual(cancelled, [])

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_committed_interrupt_during_tts_can_cancel_without_new_turn(self):
        async def run():
            started_inputs = []
            cancelled = []
            statuses = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                started_inputs.append(openai_input)
                playback_event.set()
                speaking_event.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(openai_input[-1]["content"])
                    raise

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    assistant_runner=fake_run_assistant_turn,
                    on_status=statuses.append,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Tell me something"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "audio_activity", "rms": 900})
            await scribe_events.put({"type": "commit", "text": "Stop"})
            await asyncio.sleep(0.05)

            self.assertEqual(len(started_inputs), 1)
            self.assertEqual(cancelled, ["Tell me something"])
            self.assertTrue(
                any(
                    status.get("barge_in_event_count") == 1
                    and status.get("barge_in_last_event") == "stt: hearing"
                    for status in statuses
                )
            )
            self.assertTrue(
                any(
                    status.get("barge_in_event_count") == 2
                    and status.get("barge_in_last_event") == "commit: explicit_interrupt"
                    for status in statuses
                )
            )

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_committed_interrupt_wins_even_when_it_matches_prompt_text(self):
        async def run():
            cancelled = []
            statuses = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                playback_event.set()
                speaking_event.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(openai_input[-1]["content"])
                    raise

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    assistant_runner=fake_run_assistant_turn,
                    on_status=statuses.append,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Tell me a story where someone says stop"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "audio_activity", "rms": 900})
            await scribe_events.put({"type": "commit", "text": "Stop, please."})
            await asyncio.sleep(0.05)

            self.assertEqual(cancelled, ["Tell me a story where someone says stop"])
            self.assertTrue(
                any(
                    status.get("barge_in_last_event") == "commit: explicit_interrupt"
                    for status in statuses
                )
            )

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_partial_wait_barges_while_playback_open_without_speaking_event(self):
        async def run():
            cancelled = []
            playback_stops = []
            statuses = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def stop_playback_now():
                playback_stops.append("stop")

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                playback_event.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(openai_input[-1]["content"])
                    raise

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    assistant_runner=fake_run_assistant_turn,
                    on_status=statuses.append,
                    stop_playback_now=stop_playback_now,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Tell me a story please"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "audio_activity", "rms": 900})
            await scribe_events.put({"type": "partial", "text": "Wait"})
            await asyncio.sleep(0.05)

            self.assertEqual(cancelled, ["Tell me a story please"])
            self.assertEqual(playback_stops, ["stop"])
            self.assertTrue(
                any(
                    status.get("barge_in_last_event") == "partial: explicit_interrupt"
                    for status in statuses
                )
            )

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_partial_wait_barge_in_does_not_start_new_turn(self):
        async def run():
            started_inputs = []
            cancelled = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            policy = TurnPolicy(speculative_partial_delay_secs=0, speculative_local_quiet_secs=0)

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                started_inputs.append(openai_input[-1]["content"])
                playback_event.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(openai_input[-1]["content"])
                    raise

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=policy,
                    assistant_runner=fake_run_assistant_turn,
                    stop_playback_now=lambda: None,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Tell me a story please"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "audio_activity", "rms": 900})
            await scribe_events.put({"type": "partial", "text": "wait wait wait wait wait"})
            await asyncio.sleep(0.05)

            self.assertEqual(started_inputs, ["Tell me a story please"])
            self.assertEqual(cancelled, ["Tell me a story please"])

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_rejected_partial_during_playback_restores_speaking_status(self):
        async def run():
            statuses = []
            timeline_events = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                playback_event.set()
                await asyncio.Event().wait()

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    assistant_runner=fake_run_assistant_turn,
                    on_status=statuses.append,
                    on_event=timeline_events.append,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Tell me a story please"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "audio_activity", "rms": 200})
            await scribe_events.put({"type": "partial", "text": "hi"})
            await asyncio.sleep(0.05)

            self.assertTrue(any(event.get("type") == "barge_in_rejected" for event in timeline_events))
            self.assertFalse(any(status.get("status") == "hearing" for status in statuses))
            self.assertEqual(statuses[-1].get("status"), "speaking")
            self.assertTrue(statuses[-1].get("assistant_speaking"))

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_rejected_commit_during_playback_does_not_publish_hearing(self):
        async def run():
            timeline_events = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                playback_event.set()
                await asyncio.Event().wait()

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    assistant_runner=fake_run_assistant_turn,
                    on_event=timeline_events.append,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Tell me a story please"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "audio_activity", "rms": 200})
            await scribe_events.put({"type": "commit", "text": "Actually tell me about motors"})
            await asyncio.sleep(0.05)

            self.assertTrue(any(event.get("type") == "commit_rejected" for event in timeline_events))
            self.assertFalse(
                any(
                    event.get("type") == "barge_in_fired" and event.get("reason") == "hearing"
                    for event in timeline_events
                )
            )

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_substantial_partial_during_playback_starts_new_turn(self):
        # Phase 0 scenario: assistant is speaking and the user changes topic
        # ("actually tell me about the motors please"). The current turn should be
        # cancelled, playback should stop, and a new turn should start once
        # the policy accepts the partial.
        async def run():
            started_inputs = []
            cancelled = []
            playback_stops = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            policy = TurnPolicy(
                speculative_partial_delay_secs=0,
                speculative_local_quiet_secs=0,
                barge_in_sustain_ms=0,
                assistant_speech_barge_in_cooldown_secs=0,
            )

            async def stop_playback_now():
                playback_stops.append("stop")

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                started_inputs.append(openai_input[-1]["content"])
                playback_event.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(openai_input[-1]["content"])
                    raise

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=policy,
                    assistant_runner=fake_run_assistant_turn,
                    stop_playback_now=stop_playback_now,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Tell me a story please"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "audio_activity", "rms": 900})
            await scribe_events.put({"type": "partial", "text": "actually tell me about the motors please"})
            await asyncio.sleep(0.1)

            self.assertEqual(
                started_inputs,
                ["Tell me a story please", "actually tell me about the motors please"],
            )
            self.assertEqual(cancelled, ["Tell me a story please"])
            self.assertEqual(playback_stops, ["stop"])

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_explicit_partial_interrupt_returns_to_listening(self):
        # Phase 1: an explicit interrupt ("wait") during playback must stop the
        # turn AND return the session to listening, without starting a new turn.
        async def run():
            started_inputs = []
            cancelled = []
            playback_stops = []
            statuses = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def stop_playback_now():
                playback_stops.append("stop")

            async def fake_run_assistant_turn(turn_id, openai_input, playback_event, speaking_event, openai_client, elevenlabs_api_key, voice_state, on_assistant_chunk=None, **kwargs):
                started_inputs.append(openai_input[-1]["content"])
                playback_event.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(openai_input[-1]["content"])
                    raise

            handler_task = asyncio.create_task(handle_scribe_events(
                scribe_events,
                openai_client=object(),
                elevenlabs_api_key="test-key",
                voice_state=VoiceState("test-voice"),
                stop_event=stop_event,
                    system_prompt="test system prompt",
                assistant_runner=fake_run_assistant_turn,
                on_status=statuses.append,
                stop_playback_now=stop_playback_now,
            ))

            await scribe_events.put({"type": "commit", "text": "Tell me a story please"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "audio_activity", "rms": 900})
            await scribe_events.put({"type": "partial", "text": "wait"})
            await asyncio.sleep(0.05)

            self.assertEqual(started_inputs, ["Tell me a story please"])
            self.assertEqual(cancelled, ["Tell me a story please"])
            self.assertEqual(playback_stops, ["stop"])
            self.assertEqual(statuses[-1].get("status"), "listening")

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_silent_failed_turn_clears_assistant_working(self):
        async def run():
            statuses = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                raise RuntimeError("model failed")

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    assistant_runner=fake_run_assistant_turn,
                    on_status=statuses.append,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Tell me something"})
            for _ in range(20):
                if statuses and statuses[-1].get("status") == "error":
                    break
                await asyncio.sleep(0.01)

            self.assertEqual(statuses[-1].get("status"), "error")
            self.assertFalse(statuses[-1].get("assistant_working"))
            self.assertEqual(statuses[-1].get("last_error"), "model failed")

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_substantial_commit_starting_with_interrupt_word_does_not_start_new_turn(self):
        # Phase 1: a commit that starts with an explicit interrupt word but is
        # long enough to pass commit_decision (e.g. "wait, tell me about the
        # motors please") must still stop playback and return to listening — it
        # must not start a new turn from the trailing follow-up.
        async def run():
            started_inputs = []
            cancelled = []
            playback_stops = []
            statuses = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def stop_playback_now():
                playback_stops.append("stop")

            async def fake_run_assistant_turn(turn_id, openai_input, playback_event, speaking_event, openai_client, elevenlabs_api_key, voice_state, on_assistant_chunk=None, **kwargs):
                started_inputs.append(openai_input[-1]["content"])
                playback_event.set()
                speaking_event.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(openai_input[-1]["content"])
                    raise

            handler_task = asyncio.create_task(handle_scribe_events(
                scribe_events,
                openai_client=object(),
                elevenlabs_api_key="test-key",
                voice_state=VoiceState("test-voice"),
                stop_event=stop_event,
                    system_prompt="test system prompt",
                assistant_runner=fake_run_assistant_turn,
                on_status=statuses.append,
                stop_playback_now=stop_playback_now,
            ))

            await scribe_events.put({"type": "commit", "text": "Tell me a story please"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "audio_activity", "rms": 900})
            await scribe_events.put({"type": "commit", "text": "wait tell me about the motors please"})
            await asyncio.sleep(0.05)

            self.assertEqual(started_inputs, ["Tell me a story please"])
            self.assertEqual(cancelled, ["Tell me a story please"])
            self.assertEqual(playback_stops, ["stop"])
            self.assertEqual(statuses[-1].get("status"), "listening")
            self.assertTrue(any(
                status.get("barge_in_last_event") == "commit: explicit_interrupt"
                for status in statuses
            ))

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_playback_stop_callback_cannot_block_scribe_events(self):
        async def run():
            cancelled = []
            statuses = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def stop_playback_now():
                await asyncio.Event().wait()

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                playback_event.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(openai_input[-1]["content"])
                    raise

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    assistant_runner=fake_run_assistant_turn,
                    on_status=statuses.append,
                    stop_playback_now=stop_playback_now,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Tell me a story please"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "audio_activity", "rms": 900})
            await scribe_events.put({"type": "partial", "text": "wait"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "audio_activity", "rms": 500})
            await asyncio.sleep(0.05)

            self.assertEqual(cancelled, ["Tell me a story please"])
            self.assertTrue(any(status.get("barge_in_mic_rms") == 500 for status in statuses))

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_delayed_committed_interrupt_uses_recent_barge_in_audio(self):
        async def run():
            started_inputs = []
            cancelled = []
            statuses = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                started_inputs.append(openai_input)
                playback_event.set()
                speaking_event.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(openai_input[-1]["content"])
                    raise

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    assistant_runner=fake_run_assistant_turn,
                    on_status=statuses.append,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Tell me something"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "audio_activity", "rms": 900})
            await scribe_events.put({"type": "audio_activity", "rms": 0})
            await scribe_events.put({"type": "commit", "text": "Stop"})
            await asyncio.sleep(0.05)

            self.assertEqual(len(started_inputs), 1)
            self.assertEqual(cancelled, ["Tell me something"])
            self.assertTrue(
                any(
                    status.get("barge_in_last_event") == "commit: explicit_interrupt"
                    for status in statuses
                )
            )

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_committed_interrupt_uses_utterance_audio_after_recent_window_expires(self):
        async def run():
            started_inputs = []
            cancelled = []
            statuses = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            policy = TurnPolicy(local_speech_window_secs=0.01)

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                started_inputs.append(openai_input)
                playback_event.set()
                speaking_event.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(openai_input[-1]["content"])
                    raise

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=policy,
                    assistant_runner=fake_run_assistant_turn,
                    on_status=statuses.append,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Tell me something"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "audio_activity", "rms": 900})
            await scribe_events.put({"type": "audio_activity", "rms": 0})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "commit", "text": "Stop"})
            await asyncio.sleep(0.05)

            self.assertEqual(len(started_inputs), 1)
            self.assertEqual(cancelled, ["Tell me something"])
            self.assertTrue(
                any(
                    status.get("barge_in_last_event") == "commit: explicit_interrupt"
                    for status in statuses
                )
            )

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_effective_playback_rms_decays_when_stale(self):
        from voice.assistant import effective_playback_rms

        levels = AudioLevels(playback_rms=900, playback_at=0.0)
        self.assertEqual(effective_playback_rms(levels, 0.1), 900)
        self.assertEqual(effective_playback_rms(levels, 1.0), 0)

    def test_playback_rms_scales_with_output_gain(self):
        from voice.session import playback_rms_with_gain

        self.assertEqual(playback_rms_with_gain(1000, 1.5), 1500)
        self.assertEqual(playback_rms_with_gain(30000, 2.0), 32767)

    def test_pcm16_rms_on_known_samples(self):
        from voice.turn_policy import pcm16_rms

        audio = (1000).to_bytes(2, "little", signed=True) * 2
        self.assertEqual(pcm16_rms(audio), 1000)

    def test_explicit_interrupt_still_gets_through_echo_memory(self):
        async def run():
            started_inputs = []
            cancelled = []
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                started_inputs.append(openai_input)
                if on_assistant_chunk:
                    on_assistant_chunk("Stop saying stop because that is confusing.")
                playback_event.set()
                speaking_event.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(openai_input[-1]["content"])
                    raise

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    assistant_runner=fake_run_assistant_turn,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Say stop a bunch"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "audio_activity", "rms": 900})
            await scribe_events.put({"type": "partial", "text": "Stop saying stop"})
            await asyncio.sleep(0.05)

            self.assertEqual(started_inputs[0][-1]["content"], "Say stop a bunch")
            self.assertEqual(cancelled, ["Say stop a bunch"])

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_spoken_speculative_turn_without_commit_reaches_history(self):
        async def run():
            history = ConversationHistory()
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            policy = TurnPolicy(
                speculative_partial_delay_secs=0,
                speculative_playback_delay_secs=0.01,
                speculative_local_quiet_secs=0.01,
            )

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                if on_assistant_chunk:
                    on_assistant_chunk("Batteries store energy for the robot.")
                # Real TTS stays gated on playback before it finishes.
                await playback_event.wait()
                return "Batteries store energy for the robot."

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=policy,
                    conversation_history=history,
                    assistant_runner=fake_run_assistant_turn,
                )
            )

            # Speculative trigger via a stable partial, and no commit ever arrives.
            await scribe_events.put({"type": "partial", "text": "Tell me about batteries."})
            await asyncio.sleep(0.05)

            exchanges = history.exchanges()
            self.assertEqual(len(exchanges), 1)
            self.assertEqual(exchanges[0].user_text, "Tell me about batteries.")
            self.assertEqual(exchanges[0].assistant_text, "Batteries store energy for the robot.")

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_speculative_turn_waits_for_commit_when_playback_is_disabled(self):
        async def run():
            history = ConversationHistory()
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()
            playback_opened = []
            policy = TurnPolicy(
                speculative_partial_delay_secs=0,
                speculative_local_quiet_secs=0,
                speculative_playback_enabled=False,
            )

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                if on_assistant_chunk:
                    on_assistant_chunk("Yes, batteries store energy.")
                await playback_event.wait()
                playback_opened.append(True)
                return "Yes, batteries store energy."

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    policy=policy,
                    conversation_history=history,
                    assistant_runner=fake_run_assistant_turn,
                )
            )

            await scribe_events.put({"type": "partial", "text": "Tell me about batteries."})
            await asyncio.sleep(0.05)

            self.assertEqual(playback_opened, [])
            self.assertEqual(len(history.exchanges()), 0)

            await scribe_events.put({"type": "commit", "text": "Tell me about batteries."})
            await asyncio.sleep(0.05)

            self.assertEqual(playback_opened, [True])
            exchanges = history.exchanges()
            self.assertEqual(len(exchanges), 1)
            self.assertEqual(exchanges[0].user_text, "Tell me about batteries.")
            self.assertEqual(exchanges[0].assistant_text, "Yes, batteries store energy.")

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())

    def test_barged_in_turn_records_what_it_spoke(self):
        async def run():
            history = ConversationHistory()
            scribe_events = asyncio.Queue()
            stop_event = asyncio.Event()

            async def fake_run_assistant_turn(
                turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=None,
                **kwargs,
            ):
                playback_event.set()
                if on_assistant_chunk:
                    on_assistant_chunk("Once upon a time there was a robot")
                await asyncio.Event().wait()

            handler_task = asyncio.create_task(
                handle_scribe_events(
                    scribe_events,
                    openai_client=object(),
                    elevenlabs_api_key="test-key",
                    voice_state=VoiceState("test-voice"),
                    stop_event=stop_event,
                    system_prompt="test system prompt",
                    conversation_history=history,
                    assistant_runner=fake_run_assistant_turn,
                    stop_playback_now=lambda: None,
                )
            )

            await scribe_events.put({"type": "commit", "text": "Tell me a story please"})
            await asyncio.sleep(0.05)
            await scribe_events.put({"type": "audio_activity", "rms": 900})
            await scribe_events.put({"type": "partial", "text": "Wait"})
            await asyncio.sleep(0.05)

            exchanges = history.exchanges()
            self.assertEqual(len(exchanges), 1)
            self.assertEqual(exchanges[0].user_text, "Tell me a story please")
            self.assertEqual(exchanges[0].assistant_text, "Once upon a time there was a robot")

            stop_event.set()
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

        asyncio.run(run())


class RobotToolDispatchTest(unittest.TestCase):
    def _call(self, name, call_id="call_1", arguments=None):
        return RobotToolCall(name=name, arguments=arguments or {}, call_id=call_id)

    def test_motion_tool_calls_motion_intent_caller(self):
        async def run():
            calls = []

            def motion_intent_caller(name, **_):
                calls.append(name)
                return {"ok": True, "intent": name}

            context = VoiceToolContext(
                voice_state=VoiceState("test-voice"),
                motion_intent_caller=motion_intent_caller,
            )
            result = await dispatch_tool(
                self._call(MOVE_TOOL_NAME, arguments={"distance_meters": 0.5}), context
            )

            self.assertEqual(calls, [MOVE_TOOL_NAME])
            self.assertTrue(result.ok)
            self.assertEqual(result.output, {"ok": True, "intent": MOVE_TOOL_NAME})

        asyncio.run(run())

    def test_move_distance_is_passed_through(self):
        async def run():
            calls = []

            def motion_intent_caller(name, **kwargs):
                calls.append((name, kwargs))
                return {"ok": True}

            context = VoiceToolContext(
                voice_state=VoiceState("test-voice"),
                motion_intent_caller=motion_intent_caller,
            )
            result = await dispatch_tool(
                self._call(MOVE_TOOL_NAME, arguments={"distance_meters": 0.6}), context
            )

            self.assertTrue(result.ok)
            self.assertEqual(calls[0][0], MOVE_TOOL_NAME)
            self.assertEqual(calls[0][1], {"distance_meters": 0.6})

        asyncio.run(run())

    def test_move_invalid_distance_reports_error(self):
        async def run():
            context = VoiceToolContext(
                voice_state=VoiceState("test-voice"),
                motion_intent_caller=lambda name, **kwargs: {"ok": True},
            )
            result = await dispatch_tool(
                self._call(MOVE_TOOL_NAME, arguments={"distance_meters": "far"}), context
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.output, {"ok": False, "error": "invalid_distance"})

        asyncio.run(run())

    def test_motion_tool_missing_caller_reports_error(self):
        async def run():
            context = VoiceToolContext(voice_state=VoiceState("test-voice"))
            result = await dispatch_tool(self._call(MOVE_TOOL_NAME), context)
            self.assertFalse(result.ok)
            self.assertEqual(result.output, {"ok": False, "error": "motion_caller_missing"})

        asyncio.run(run())

    def test_look_produces_image_parts(self):
        async def run():
            context = VoiceToolContext(
                voice_state=VoiceState("test-voice"),
                camera_snapshot_caller=lambda: b"jpeg-bytes",
            )
            result = await dispatch_tool(self._call(LOOK_TOOL_NAME), context)

            self.assertTrue(result.ok)
            self.assertEqual(result.output, {"ok": True, "image_attached": True})
            self.assertEqual(result.image_parts[0]["type"], "input_text")
            self.assertEqual(result.image_parts[1]["type"], "input_image")
            self.assertTrue(result.image_parts[1]["image_url"].startswith("data:image/jpeg;base64,"))

        asyncio.run(run())

    def test_look_image_becomes_agent_observation(self):
        async def run():
            context = VoiceToolContext(
                voice_state=VoiceState("test-voice"),
                camera_snapshot_caller=lambda: b"jpeg-bytes",
            )
            result = await dispatch_tool(self._call(LOOK_TOOL_NAME), context)
            observation = agent_observation(result)

            # The image rides as a real input_image part, not base64 in the text.
            self.assertEqual(observation.input_parts[1]["type"], "input_image")
            self.assertNotIn("base64", observation.text)

        asyncio.run(run())

    def test_check_health_returns_battery_not_surroundings(self):
        async def run():
            context = VoiceToolContext(
                voice_state=VoiceState("test-voice"),
                robot_inspection_caller=lambda: {
                    "sources": {"gamepad_teleop": {"stale": False}},
                    "motor_battery": {"status": "ok", "pack_voltage": 11.8, "cell_voltage": 3.93},
                },
            )
            result = await dispatch_tool(self._call(CHECK_HEALTH_TOOL_NAME), context)

            self.assertTrue(result.ok)
            self.assertEqual(result.output["battery"]["status"], "ok")
            self.assertNotIn("sensors", result.output)

        asyncio.run(run())

    def test_check_surroundings_returns_sensors_not_battery(self):
        async def run():
            context = VoiceToolContext(
                voice_state=VoiceState("test-voice"),
                robot_inspection_caller=lambda: {
                    "sources": {"gamepad_teleop": {"stale": False}},
                    "motor_battery": {"status": "ok", "pack_voltage": 11.8, "cell_voltage": 3.93},
                },
            )
            result = await dispatch_tool(self._call(CHECK_SURROUNDINGS_TOOL_NAME), context)

            self.assertTrue(result.ok)
            self.assertFalse(result.output["sensors"]["available"])
            self.assertNotIn("battery", result.output)

        asyncio.run(run())

    def test_scan_caps_total_sweep_at_one_full_turn(self):
        async def run():
            turns = []

            def motion_intent_caller(name, **kwargs):
                turns.append(kwargs.get("degrees"))
                return {"ok": True}

            context = VoiceToolContext(
                voice_state=VoiceState("test-voice"),
                camera_snapshot_caller=lambda: b"jpeg-bytes",
                motion_intent_caller=motion_intent_caller,
            )
            # A wildly oversized argument must not turn into a hundred sequential turns.
            result = await dispatch_tool(self._call("scan", arguments={"degrees": 10000}), context)

            self.assertTrue(result.ok)
            self.assertEqual(result.output["degrees_covered"], 360)
            self.assertEqual(len(turns), 4)

        asyncio.run(run())

    def test_scan_returns_to_starting_heading_after_partial_sweep(self):
        async def run():
            turns = []

            def motion_intent_caller(name, **kwargs):
                turns.append(kwargs.get("degrees"))
                return {"ok": True}

            context = VoiceToolContext(
                voice_state=VoiceState("test-voice"),
                camera_snapshot_caller=lambda: b"jpeg-bytes",
                motion_intent_caller=motion_intent_caller,
            )
            result = await dispatch_tool(self._call("scan", arguments={"degrees": 180}), context)

            self.assertTrue(result.ok)
            # Two snapshot turns of +90, then a corrective turn back to start so the
            # snapshot labels stay true from where the robot now sits.
            self.assertEqual(turns[:2], [90, 90])
            self.assertEqual(sum(turns), 0)

        asyncio.run(run())

    def test_face_me_calls_caller(self):
        async def run():
            calls = []

            def face_me_caller():
                calls.append(True)
                return {"ok": True, "result": "completed", "relative_degrees": 90}

            context = VoiceToolContext(
                voice_state=VoiceState("test-voice"),
                face_me_caller=face_me_caller,
            )
            result = await dispatch_tool(self._call(FACE_ME_TOOL_NAME), context)

            self.assertEqual(len(calls), 1)
            self.assertEqual(result.output, {"ok": True, "result": "completed", "relative_degrees": 90})

        asyncio.run(run())

    def test_end_session_sets_pending_flag(self):
        async def run():
            pending = [False]
            context = VoiceToolContext(voice_state=VoiceState("test-voice"), end_session_pending=pending)
            result = await dispatch_tool(self._call(END_SESSION_TOOL_NAME), context)

            self.assertTrue(pending[0])
            self.assertEqual(result.output, {"ok": True, "ended": True})

        asyncio.run(run())

    def test_unknown_tool_reports_unsupported(self):
        async def run():
            context = VoiceToolContext(voice_state=VoiceState("test-voice"))
            result = await dispatch_tool(self._call("not_a_real_tool"), context)
            self.assertFalse(result.ok)
            self.assertEqual(result.output, {"ok": False, "error": "unsupported tool"})

        asyncio.run(run())

    def test_inspect_speaker_direction_fresh(self):
        async def run():
            context = VoiceToolContext(
                voice_state=VoiceState("test-voice"),
                speaker_direction_caller=lambda: {
                    "connected": True,
                    "relative_degrees": 30,
                    "age_seconds": 1.2,
                    "fresh": True,
                },
            )
            result = await dispatch_tool(self._call(INSPECT_SPEAKER_DIRECTION_TOOL_NAME), context)
            self.assertEqual(
                result.output,
                {"ok": True, "available": True, "fresh": True, "relative_degrees": 30, "age_seconds": 1.2},
            )

        asyncio.run(run())

    def test_inspect_speaker_direction_stale(self):
        async def run():
            context = VoiceToolContext(
                voice_state=VoiceState("test-voice"),
                speaker_direction_caller=lambda: {
                    "connected": True,
                    "relative_degrees": -45,
                    "age_seconds": 30.0,
                    "fresh": False,
                },
            )
            result = await dispatch_tool(self._call(INSPECT_SPEAKER_DIRECTION_TOOL_NAME), context)
            self.assertTrue(result.output["available"])
            self.assertFalse(result.output["fresh"])
            self.assertEqual(result.output["relative_degrees"], -45)

        asyncio.run(run())

    def test_inspect_speaker_direction_unavailable_without_cache(self):
        async def run():
            context = VoiceToolContext(
                voice_state=VoiceState("test-voice"),
                speaker_direction_caller=lambda: {
                    "connected": True,
                    "relative_degrees": None,
                    "age_seconds": None,
                    "fresh": False,
                },
            )
            result = await dispatch_tool(self._call(INSPECT_SPEAKER_DIRECTION_TOOL_NAME), context)
            self.assertEqual(result.output, {"ok": True, "available": False, "fresh": False})

        asyncio.run(run())

    def test_inspect_speaker_direction_unavailable_without_caller(self):
        async def run():
            context = VoiceToolContext(voice_state=VoiceState("test-voice"))
            result = await dispatch_tool(self._call(INSPECT_SPEAKER_DIRECTION_TOOL_NAME), context)
            self.assertFalse(result.ok)
            self.assertEqual(result.output, {"ok": False, "error": "speaker_direction_unavailable"})

        asyncio.run(run())

    def test_parse_tool_arguments_handles_blank_and_invalid(self):
        self.assertEqual(parse_tool_arguments(""), {})
        self.assertEqual(parse_tool_arguments(None), {})
        self.assertEqual(parse_tool_arguments("not json"), {})
        self.assertEqual(parse_tool_arguments('{"a": 1}'), {"a": 1})


if __name__ == "__main__":
    unittest.main()
