import os
import sys
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from voice.usage import (
    ELEVEN_FLASH_MODEL,
    OPENAI_MODEL,
    SCRIBE_MODEL,
    UsageTotals,
    cost_snapshot,
    record_openai_usage,
)


class VoiceUsageTest(unittest.TestCase):
    def test_record_openai_usage_splits_cached_input(self):
        usage = UsageTotals()
        openai_usage = SimpleNamespace(
            input_tokens=1000,
            output_tokens=200,
            input_tokens_details=SimpleNamespace(cached_tokens=400),
        )

        record_openai_usage(usage, openai_usage)
        record_openai_usage(usage, openai_usage)

        self.assertEqual(usage.llm_input_tokens, 2000)
        self.assertEqual(usage.llm_cached_input_tokens, 800)
        self.assertEqual(usage.llm_output_tokens, 400)

    def test_record_openai_usage_tolerates_missing_fields(self):
        usage = UsageTotals()
        record_openai_usage(usage, None)
        record_openai_usage(usage, SimpleNamespace())

        self.assertEqual(usage.llm_input_tokens, 0)
        self.assertEqual(usage.llm_cached_input_tokens, 0)
        self.assertEqual(usage.llm_output_tokens, 0)

    def test_cost_snapshot_prices_each_category(self):
        usage = UsageTotals(
            stt_audio_seconds=60.0,
            llm_input_tokens=1_000_000,
            llm_cached_input_tokens=200_000,
            llm_output_tokens=1_000_000,
            tts_characters=1000,
        )

        snapshot = cost_snapshot(usage)

        self.assertEqual(snapshot["stt"]["model"], SCRIBE_MODEL)
        self.assertEqual(snapshot["llm"]["model"], OPENAI_MODEL)
        self.assertEqual(snapshot["tts"]["model"], ELEVEN_FLASH_MODEL)
        # 1 minute of STT at $0.080/minute.
        self.assertAlmostEqual(snapshot["stt"]["usd"], 0.080)
        # 0.8M uncached input @ $0.75 + 0.2M cached @ $0.075 + 1M output @ $4.50.
        self.assertAlmostEqual(snapshot["llm"]["usd"], 0.6 + 0.015 + 4.50)
        # 1000 chars at $0.05/1000.
        self.assertAlmostEqual(snapshot["tts"]["usd"], 0.050)
        self.assertAlmostEqual(
            snapshot["total_usd"],
            snapshot["stt"]["usd"] + snapshot["llm"]["usd"] + snapshot["tts"]["usd"],
        )

    def test_cost_snapshot_empty_is_zero(self):
        snapshot = cost_snapshot(UsageTotals())

        self.assertEqual(snapshot["total_usd"], 0.0)
        self.assertEqual(snapshot["llm"]["input_tokens"], 0)
        self.assertEqual(snapshot["tts"]["characters"], 0)


if __name__ == "__main__":
    unittest.main()
