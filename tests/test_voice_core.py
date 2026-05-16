import asyncio
import os
import sys
import unittest
from contextlib import suppress
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from voice.assistant import ActiveTurn, VoiceState, VoiceSwitch, handle_scribe_events, stream_openai_words
from voice.conversation import ConversationHistory
from voice.turn_policy import TurnPolicy, should_accept_barge_in, should_speculate, transcript_matches


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
                local_speech_recent=True,
            )
        )

    def test_assistant_echo_partial_while_speaking_does_not_barge_in(self):
        should_barge_in, reason = TurnPolicy().barge_in_decision(
            "Sure. Here's a tiny one.",
            assistant_speaking=True,
            local_speech_recent=True,
            assistant_speech_elapsed_secs=1.0,
            local_speech_rms=800,
            assistant_text="Sure, here's a tiny one: a small light can matter a lot.",
        )

        self.assertFalse(should_barge_in)
        self.assertEqual(reason, "assistant_echo")


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

    def test_completed_committed_turn_is_available_to_next_openai_request(self):
        async def run():
            started_inputs = []
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


if __name__ == "__main__":
    unittest.main()
