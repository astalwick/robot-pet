import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config.voice import VoiceConfig
from voice.assistant import ASSISTANT_TOOLS, OPERATIONAL_SYSTEM_PROMPT, compose_system_prompt
from voice.session import VoiceSession


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
