import os
import sys
import unittest

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
        self.assertEqual(session.voice_state.voice_id, "scientist-voice")
        self.assertIn("Scientist observes first.", session.system_prompt)
        self.assertIn(OPERATIONAL_SYSTEM_PROMPT, session.system_prompt)
        self.assertEqual(session.system_prompt, compose_system_prompt("Scientist observes first."))

    def test_unknown_personality_falls_back_to_default_card(self):
        session = make_session("missing")

        self.assertEqual(session.personality_name, "default")
        self.assertEqual(session.voice_state.voice_id, "default-voice")
        self.assertIn("Default character prose.", session.system_prompt)
        self.assertEqual(session.system_prompt, compose_system_prompt("Default character prose."))


class AssistantToolsTest(unittest.TestCase):
    def test_assistant_tools_exclude_switch_voice(self):
        tool_names = {tool["name"] for tool in ASSISTANT_TOOLS if tool.get("type") == "function"}
        self.assertNotIn("switch_voice", tool_names)
        self.assertEqual(tool_names, {"end_session", "wiggle", "move_forward", "look_around"})
