import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from voice.assistant import OPERATIONAL_SYSTEM_PROMPT, compose_system_prompt
from voice.personality import (
    DEFAULT_PROSE,
    DEFAULT_VOICE_ID,
    load_personalities,
    lookup_personality,
)


class PersonalityLoaderTest(unittest.TestCase):
    def test_loads_voice_id_and_prose(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            card = Path(tmpdir) / "stoic.md"
            card.write_text(
                "---\n"
                "voice_id: voice-abc\n"
                "---\n"
                "\n"
                "Short answers only.\n"
            )

            personalities = load_personalities(tmpdir)

        self.assertEqual(personalities["stoic"], ("voice-abc", "Short answers only.\n"))

    def test_skips_readme_and_cards_without_voice_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "README.md").write_text("# Personality cards\n")
            Path(tmpdir, "broken.md").write_text("No frontmatter here.\n")
            Path(tmpdir, "good.md").write_text("---\nvoice_id: ok\n---\n\nBody.\n")

            personalities = load_personalities(tmpdir)

        self.assertEqual(list(personalities.keys()), ["good"])
        self.assertEqual(personalities["good"][1], "Body.\n")

    def test_missing_directory_returns_empty_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = os.path.join(tmpdir, "nope")

        self.assertEqual(load_personalities(missing), {})

    def test_lookup_falls_back_to_builtin_default(self):
        resolved_name, voice_id, prose = lookup_personality("missing", {})

        self.assertEqual(resolved_name, "default")
        self.assertEqual(voice_id, DEFAULT_VOICE_ID)
        self.assertEqual(prose, DEFAULT_PROSE)

    def test_lookup_returns_named_card(self):
        personalities = {"stoic": ("voice-abc", "Be brief.")}

        resolved_name, voice_id, prose = lookup_personality("stoic", personalities)

        self.assertEqual(resolved_name, "stoic")
        self.assertEqual(voice_id, "voice-abc")
        self.assertEqual(prose, "Be brief.")

    def test_repo_default_card_loads(self):
        personalities = load_personalities(os.path.join(ROOT, "config", "personality"))

        self.assertIn("default", personalities)
        self.assertEqual(personalities["default"][0], DEFAULT_VOICE_ID)
        self.assertIn("Bloop", personalities["default"][1])

    def test_compose_system_prompt_puts_operational_last(self):
        prompt = compose_system_prompt("You are a quiet robot.")

        self.assertTrue(prompt.startswith("You are a quiet robot."))
        self.assertIn(OPERATIONAL_SYSTEM_PROMPT, prompt)
        self.assertLess(prompt.index("You are a quiet robot."), prompt.index("# Operational System Prompt"))
