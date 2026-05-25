import asyncio
import json
import os
import sys
import unittest
from contextlib import suppress
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from voice.assistant import (
    END_SESSION_TOOL_NAME,
    ActiveTurn,
    VoiceState,
    VoiceSwitch,
    handle_scribe_events,
    note_mic_chunk,
    refresh_barge_in_gate,
    stream_openai_words,
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
        levels: dict[str, float | int] = {"mic_peak": 0, "mic_last": 0}
        note_mic_chunk(levels, 120)
        note_mic_chunk(levels, 450)
        note_mic_chunk(levels, 200)
        self.assertEqual(levels["mic_peak"], 450)
        self.assertEqual(levels["mic_last"], 200)

    def test_refresh_barge_in_gate_writes_threshold_and_gate(self):
        policy = TurnPolicy(barge_in_min_rms=500)
        levels = {"playback_rms": 0, "playback_at": 0.0}
        _, gate_open, threshold, reason = refresh_barge_in_gate(levels, 0.0, policy, False, 100)
        self.assertFalse(gate_open)
        self.assertEqual(threshold, 500)
        self.assertEqual(reason, "assistant_not_speaking")
        self.assertEqual(levels["threshold_rms"], 500)

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
    def test_stream_openai_words_yields_voice_switch_for_tool_call(self):
        async def run():
            class FakeResponses:
                def __init__(self):
                    self.calls = []

                async def create(self, **kwargs):
                    self.calls.append(kwargs)
                    if len(self.calls) == 1:
                        events = [
                            SimpleNamespace(type="response.output_text.delta", delta="Switching "),
                            SimpleNamespace(
                                type="response.output_item.done",
                                item=SimpleNamespace(type="function_call", name="switch_voice", call_id="call_1"),
                            ),
                            SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_1")),
                        ]
                    else:
                        events = [
                            SimpleNamespace(type="response.output_text.delta", delta="voices."),
                            SimpleNamespace(type="response.completed", response=SimpleNamespace(id="resp_2")),
                        ]

                    async def stream():
                        for event in events:
                            yield event

                    return stream()

            fake_responses = FakeResponses()
            voice_state = VoiceState("default-voice", "alternate-voice", "default-voice")

            chunks = [
                chunk
                async for chunk in stream_openai_words(
                    [{"role": "user", "content": "Switch voices"}],
                    SimpleNamespace(responses=fake_responses),
                    voice_state,
                )
            ]

            self.assertEqual(chunks[0], "Switching ")
            self.assertEqual(chunks[1], VoiceSwitch(voice_id="alternate-voice", voice_name="alternate"))
            self.assertEqual(chunks[2], "voices.")
            self.assertEqual(fake_responses.calls[1]["previous_response_id"], "resp_1")

        asyncio.run(run())

    def test_stream_openai_words_calls_session_end_caller_for_tool(self):
        async def run():
            ended = []

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
            voice_state = VoiceState("default-voice", "alternate-voice", "default-voice")

            chunks = [
                chunk
                async for chunk in stream_openai_words(
                    [{"role": "user", "content": "Goodbye"}],
                    SimpleNamespace(responses=fake_responses),
                    voice_state,
                    session_end_caller=lambda: ended.append(True),
                )
            ]

            self.assertEqual(ended, [True])
            self.assertEqual(chunks, ["Bye."])
            tool_output = json.loads(fake_responses.calls[1]["input"][0]["output"])
            self.assertEqual(tool_output, {"ok": True, "ended": True})

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
                    voice_state=VoiceState("test-voice", "alternate-test-voice", "test-voice"),
                    stop_event=stop_event,
                    policy=policy,
                    conversation_history=history,
                    system_prompt="test system prompt",
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
                    voice_state=VoiceState("test-voice", "alternate-test-voice", "test-voice"),
                    stop_event=stop_event,
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
                    voice_state=VoiceState("test-voice", "alternate-test-voice", "test-voice"),
                    stop_event=stop_event,
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
                    voice_state=VoiceState("test-voice", "alternate-test-voice", "test-voice"),
                    stop_event=stop_event,
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
                    voice_state=VoiceState("test-voice", "alternate-test-voice", "test-voice"),
                    stop_event=stop_event,
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
                    voice_state=VoiceState("test-voice", "alternate-test-voice", "test-voice"),
                    stop_event=stop_event,
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
                    voice_state=VoiceState("test-voice", "alternate-test-voice", "test-voice"),
                    stop_event=stop_event,
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
                    voice_state=VoiceState("test-voice", "alternate-test-voice", "test-voice"),
                    stop_event=stop_event,
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
                    voice_state=VoiceState("test-voice", "alternate-test-voice", "test-voice"),
                    stop_event=stop_event,
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
                    voice_state=VoiceState("test-voice", "alternate-test-voice", "test-voice"),
                    stop_event=stop_event,
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
                    voice_state=VoiceState("test-voice", "alternate-test-voice", "test-voice"),
                    stop_event=stop_event,
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

    def test_effective_playback_rms_decays_when_stale(self):
        from voice.assistant import effective_playback_rms

        levels = {"playback_rms": 900, "playback_at": 0.0}
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
                    voice_state=VoiceState("test-voice", "alternate-test-voice", "test-voice"),
                    stop_event=stop_event,
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


if __name__ == "__main__":
    unittest.main()
