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

    def test_save_load_round_trip_preserves_optional_strings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "voice.json")
            config = VoiceConfig(
                enabled=True,
                input_device="hw:1,0",
                output_device="plughw:1,0",
                capture_channel_index=0,
                voice_id="voice-a",
                alternate_voice_id="voice-b",
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


if __name__ == "__main__":
    unittest.main()
