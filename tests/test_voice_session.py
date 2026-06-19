import asyncio
import os
import sys
import unittest
from contextlib import suppress
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config.voice import VoiceConfig
from voice.assistant import (
    ASSISTANT_TOOLS,
    OPERATIONAL_SYSTEM_PROMPT,
    AgentGoalRequest,
    ConversationHistory,
    VoiceState,
    compose_system_prompt,
    handle_scribe_events,
)
from voice.session import VoiceSession
from voice.turn_policy import TurnPolicy


CARD_MAP = {
    "default": ("default-voice", "Default character prose."),
    "scientist": ("scientist-voice", "Scientist observes first."),
}


def make_session(personality: str, card_map: dict[str, tuple[str, str]] | None = None) -> VoiceSession:
    return VoiceSession(
        VoiceConfig(personality=personality),
        "test-elevenlabs-key",
        object(),
        lambda _update: None,
        object(),
        personalities=card_map if card_map is not None else CARD_MAP,
    )


class VoiceSessionPersonalityTest(unittest.TestCase):
    def test_uses_selected_personality_card(self):
        session = make_session("scientist")

        self.assertEqual(session.personality_name, "scientist")
        self.assertEqual(session.voice_state.current_voice_id, "scientist-voice")
        self.assertIn("Scientist observes first.", session.system_prompt)
        self.assertIn(OPERATIONAL_SYSTEM_PROMPT, session.system_prompt)
        self.assertEqual(session.system_prompt, compose_system_prompt("Scientist observes first."))

    def test_unknown_personality_falls_back_to_default_card(self):
        session = make_session("missing")

        self.assertEqual(session.personality_name, "default")
        self.assertEqual(session.voice_state.current_voice_id, "default-voice")
        self.assertIn("Default character prose.", session.system_prompt)
        self.assertEqual(session.system_prompt, compose_system_prompt("Default character prose."))

    def test_set_personality_switches_card_and_voice(self):
        session = make_session("default")
        self.assertEqual(session.personality_name, "default")

        session.set_personality("scientist")

        self.assertEqual(session.personality_name, "scientist")
        self.assertEqual(session.voice_state.current_voice_id, "scientist-voice")
        self.assertEqual(session.voice_state.default_voice_id, "scientist-voice")
        self.assertEqual(session.system_prompt, compose_system_prompt("Scientist observes first."))

    def test_set_unknown_personality_falls_back_to_default_card(self):
        session = make_session("scientist")

        session.set_personality("missing")

        self.assertEqual(session.personality_name, "default")
        self.assertEqual(session.voice_state.current_voice_id, "default-voice")
        self.assertEqual(session.system_prompt, compose_system_prompt("Default character prose."))

    def test_card_voice_drives_initial_voice_state(self):
        session = VoiceSession(
            VoiceConfig(personality="scientist", voice_id="default-voice-id", alternate_voice_id="alt-voice-id"),
            "test-elevenlabs-key",
            object(),
            lambda _update: None,
            object(),
            personalities=CARD_MAP,
        )

        self.assertEqual(session.voice_state.default_voice_id, "scientist-voice")
        self.assertEqual(session.voice_state.alternate_voice_id, "alt-voice-id")
        self.assertEqual(session.voice_state.current_voice_id, "scientist-voice")

    def test_start_passes_configured_openai_model_to_scribe_handler(self):
        class FakeAudio:
            def mic_frames(self, _stop_event):
                return object()

        session = VoiceSession(
            VoiceConfig(personality="scientist", openai_model="gpt-5.5"),
            "test-elevenlabs-key",
            object(),
            lambda _update: None,
            FakeAudio(),
            personalities=CARD_MAP,
        )

        async def run():
            async def fake_scribe_streamer(*_args, **_kwargs):
                await session.stop_event.wait()

            async def fake_handle_scribe_events(*_args, **kwargs):
                self.assertEqual(kwargs["openai_model"], "gpt-5.5")
                await session.stop_event.wait()

            session.scribe_streamer = fake_scribe_streamer
            with mock.patch("voice.session.handle_scribe_events", fake_handle_scribe_events):
                await session.start()
                await session.stop()

        import asyncio

        asyncio.run(run())


class AssistantToolsTest(unittest.TestCase):
    def test_assistant_tools_include_switch_voice(self):
        tool_names = {tool["name"] for tool in ASSISTANT_TOOLS if tool.get("type") == "function"}
        self.assertIn("switch_voice", tool_names)
        self.assertEqual(
            tool_names,
            {"switch_voice", "end_session", "wiggle", "move_forward", "look_around", "inspect_robot", "face_me", "start_goal"},
        )


class GoalProgressSpeechTest(unittest.TestCase):
    """Barge-in and cancellation against a goal that is speaking progress narration.

    A goal speaks its narration through the injected progress speaker, which the
    orchestration loop registers as the current interruptible playback. These tests
    drive handle_scribe_events directly so the progress speaker is real, while the
    goal runner and TTS are fakes.
    """

    def _harness(self):
        playing = asyncio.Event()
        stop_playback_calls: list[bool] = []
        events: list[dict] = []
        statuses: list[dict] = []
        history = ConversationHistory()
        scribe_events: asyncio.Queue = asyncio.Queue()
        stop_event = asyncio.Event()

        async def fake_run_assistant_turn(
            turn_id, openai_input, playback_event, speaking_event,
            openai_client, elevenlabs_api_key, voice_state, on_assistant_chunk=None, **kwargs,
        ):
            return AgentGoalRequest(goal="come here")

        async def fake_progress_tts(text_chunks, api_key, voice_id, playback_event, speaking_event):
            # Mirror real TTS: hold speaking state and the playback open until cancelled.
            speaking_event.set()
            playing.set()
            try:
                await asyncio.Event().wait()
            finally:
                speaking_event.clear()

        async def fake_goal_runner(*, goal, stop_event, speak_progress, is_speaking, **kwargs):
            await speak_progress("I am on my way over to you now.")
            return ""

        def stop_playback_now():
            stop_playback_calls.append(True)

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
                progress_speaker=fake_progress_tts,
                stop_playback_now=stop_playback_now,
                on_event=events.append,
                on_status=statuses.append,
            )
        )
        return {
            "playing": playing,
            "stop_playback_calls": stop_playback_calls,
            "events": events,
            "statuses": statuses,
            "history": history,
            "scribe_events": scribe_events,
            "stop_event": stop_event,
            "handler_task": handler_task,
        }

    async def _shutdown(self, h):
        h["stop_event"].set()
        h["handler_task"].cancel()
        with suppress(asyncio.CancelledError):
            await h["handler_task"]

    async def _wait_until(self, predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0.01)

    def test_partial_stop_during_progress_speech_stops_playback(self):
        async def run():
            h = self._harness()
            await h["scribe_events"].put({"type": "commit", "text": "Come over here."})
            await asyncio.wait_for(h["playing"].wait(), 1.0)

            # Raise the mic so the explicit interrupt is accepted, then say "stop".
            await h["scribe_events"].put({"type": "audio_activity", "rms": 4000})
            await h["scribe_events"].put({"type": "partial", "text": "stop"})

            await self._wait_until(lambda: h["stop_playback_calls"])
            self.assertTrue(h["stop_playback_calls"])
            self.assertTrue(any(event["type"] == "goal_cancel" for event in h["events"]))

            await self._shutdown(h)

        asyncio.run(run())

    def test_committed_stop_during_progress_speech_cancels_goal_and_listens(self):
        async def run():
            h = self._harness()
            await h["scribe_events"].put({"type": "commit", "text": "Come over here."})
            await asyncio.wait_for(h["playing"].wait(), 1.0)

            await h["scribe_events"].put({"type": "commit", "text": "Stop."})
            await self._wait_until(lambda: any(e["type"] == "goal_cancel" for e in h["events"]))

            self.assertTrue(any(event["type"] == "goal_cancel" for event in h["events"]))
            self.assertEqual(list(h["history"].exchanges()), [])
            last_status = [s for s in h["statuses"] if "status" in s][-1]
            self.assertEqual(last_status["status"], "listening")

            await self._shutdown(h)

        asyncio.run(run())

    def test_committed_echo_of_progress_speech_does_not_cancel_goal(self):
        async def run():
            h = self._harness()
            await h["scribe_events"].put({"type": "commit", "text": "Come over here."})
            await asyncio.wait_for(h["playing"].wait(), 1.0)

            # Scribe transcribes the robot's own narration and commits it. This must
            # be suppressed as echo, not treated as user speech that cancels the goal.
            await h["scribe_events"].put({"type": "commit", "text": "I am on my way over to you now."})
            await self._wait_until(lambda: any(e["type"] == "echo_suppressed" for e in h["events"]))

            self.assertTrue(any(event["type"] == "echo_suppressed" for event in h["events"]))
            self.assertFalse(any(event["type"] == "goal_cancel" for event in h["events"]))

            await self._shutdown(h)

        asyncio.run(run())

    def test_shutdown_during_progress_speech_stops_the_goal(self):
        async def run():
            h = self._harness()
            await h["scribe_events"].put({"type": "commit", "text": "Come over here."})
            await asyncio.wait_for(h["playing"].wait(), 1.0)

            await self._shutdown(h)

            # The progress speaker was cancelled on shutdown, which stops playback.
            self.assertTrue(h["stop_playback_calls"])

        asyncio.run(run())
