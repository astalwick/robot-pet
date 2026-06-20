import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config.voice import VoiceConfig, VoiceConfigError, load_voice_config, save_voice_config


class VoiceConfigTest(unittest.TestCase):
    def test_missing_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_voice_config(os.path.join(tmpdir, "voice.json"))

        self.assertEqual(config, VoiceConfig())

    def test_save_load_round_trip_preserves_personality(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "voice.json")
            config = VoiceConfig(
                enabled=True,
                input_device="hw:1,0",
                output_device="plughw:1,0",
                capture_channel_index=0,
                input_gain=1.5,
                output_gain=0.7,
                personality="stoic",
            )

            save_voice_config(config, path)

            self.assertEqual(load_voice_config(path), config)

    def test_malformed_json_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "voice.json")
            with open(path, "w") as file_obj:
                file_obj.write("{")

            with self.assertRaisesRegex(VoiceConfigError, "Invalid voice config"):
                load_voice_config(path)

    def test_non_object_json_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "voice.json")
            with open(path, "w") as file_obj:
                json.dump([1, 2, 3], file_obj)

            with self.assertRaisesRegex(VoiceConfigError, "expected a JSON object"):
                load_voice_config(path)

    def test_channel_index_validation(self):
        with self.assertRaisesRegex(VoiceConfigError, "capture_channel_index"):
            VoiceConfig.from_dict({"capture_channel_index": 6})

    def test_only_initial_audio_shape_is_allowed(self):
        with self.assertRaisesRegex(VoiceConfigError, "sample_rate"):
            VoiceConfig.from_dict({"sample_rate": 48000})
        with self.assertRaisesRegex(VoiceConfigError, "capture_channels"):
            VoiceConfig.from_dict({"capture_channels": 2})
        with self.assertRaisesRegex(VoiceConfigError, "output_channels"):
            VoiceConfig.from_dict({"output_channels": 2})

    def test_audio_gains_are_clamped(self):
        self.assertEqual(VoiceConfig.from_dict({"input_gain": -1, "output_gain": 9}).input_gain, 0.0)
        self.assertEqual(VoiceConfig.from_dict({"input_gain": -1, "output_gain": 9}).output_gain, 3.0)

    def test_openai_model_must_be_known(self):
        self.assertEqual(VoiceConfig.from_dict({"openai_model": "gpt-5.5"}).openai_model, "gpt-5.5")
        with self.assertRaisesRegex(VoiceConfigError, "openai_model"):
            VoiceConfig.from_dict({"openai_model": "gpt-9"})

    def test_wake_threshold_validation(self):
        with self.assertRaisesRegex(VoiceConfigError, "wake_threshold"):
            VoiceConfig.from_dict({"wake_threshold": 1.5})

    def test_wake_config_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "voice.json")
            config = VoiceConfig(
                enabled=True,
                wake_word_enabled=True,
                wake_word_model_path="/tmp/hey.onnx",
                wake_threshold=0.42,
                wake_debounce_secs=3.0,
                wake_chime_path="/tmp/chime.wav",
                session_idle_secs=45.0,
            )
            save_voice_config(config, path)
            self.assertEqual(load_voice_config(path), config)

    def test_voice_id_fields_are_preserved(self):
        config = VoiceConfig.from_dict(
            {
                "voice_id": "legacy-voice",
                "alternate_voice_id": "legacy-alt",
                "personality": "stoic",
            }
        )
        self.assertEqual(config.personality, "stoic")
        self.assertEqual(config.voice_id, "legacy-voice")
        self.assertEqual(config.alternate_voice_id, "legacy-alt")
        self.assertEqual(config.to_dict()["voice_id"], "legacy-voice")
        self.assertEqual(config.to_dict()["alternate_voice_id"], "legacy-alt")

    def test_explicit_interrupt_words_parse_from_config_string(self):
        from voice.turn_policy import turn_policy_from_config

        config = VoiceConfig.from_dict({"barge_in_explicit_interrupts": "halt, Stop"})
        policy = turn_policy_from_config(config)
        self.assertEqual(policy.explicit_interrupt_words, frozenset({"halt", "stop"}))

    def test_speculative_playback_flag_reaches_turn_policy(self):
        from voice.turn_policy import turn_policy_from_config

        self.assertFalse(turn_policy_from_config(VoiceConfig()).speculative_playback_enabled)
        self.assertTrue(
            turn_policy_from_config(
                VoiceConfig.from_dict({"speculative_playback_enabled": True})
            ).speculative_playback_enabled
        )


if __name__ == "__main__":
    unittest.main()
