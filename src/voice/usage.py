"""Session API usage and cost tracking for the voice assistant.

Totals accumulate for the life of the robot-voice process (one robot session)
and reset on restart — we don't persist anything. Prices are the providers'
pay-as-you-go API rates; update the constants below when they change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from voice.assistant import OPENAI_MODEL
from voice.elevenlabs_io import ELEVEN_FLASH_MODEL, SCRIBE_MODEL


# Pay-as-you-go API pricing in USD. Sourced 2026-06-01.
# OpenAI gpt-5.4-mini: https://openai.com/api/pricing/  (per 1M tokens)
LLM_INPUT_USD_PER_MTOK = 0.75
LLM_CACHED_INPUT_USD_PER_MTOK = 0.075
LLM_OUTPUT_USD_PER_MTOK = 4.50
# ElevenLabs Scribe v2 realtime STT: https://elevenlabs.io/pricing/api  (per audio minute)
STT_USD_PER_MINUTE = 0.080
# ElevenLabs Flash v2.5 TTS: https://elevenlabs.io/pricing/api  (per 1000 characters)
TTS_USD_PER_1K_CHARS = 0.050


@dataclass
class UsageTotals:
    stt_audio_seconds: float = 0.0
    llm_input_tokens: int = 0
    llm_cached_input_tokens: int = 0
    llm_output_tokens: int = 0
    tts_characters: int = 0


def record_openai_usage(usage: UsageTotals, openai_usage: Any) -> None:
    """Add token counts from an OpenAI Responses ``response.usage`` object.

    ``input_tokens`` is the full input including the cached portion; we keep the
    cached count separately so the dashboard can price it at the lower rate.
    """
    if openai_usage is None:
        return
    usage.llm_input_tokens += getattr(openai_usage, "input_tokens", 0) or 0
    usage.llm_output_tokens += getattr(openai_usage, "output_tokens", 0) or 0
    details = getattr(openai_usage, "input_tokens_details", None)
    usage.llm_cached_input_tokens += getattr(details, "cached_tokens", 0) or 0


def cost_snapshot(usage: UsageTotals) -> dict[str, Any]:
    """Build the per-category usage + cost block published to the dashboard."""
    stt_usd = usage.stt_audio_seconds / 60.0 * STT_USD_PER_MINUTE
    uncached_input = max(0, usage.llm_input_tokens - usage.llm_cached_input_tokens)
    llm_usd = (
        uncached_input * LLM_INPUT_USD_PER_MTOK
        + usage.llm_cached_input_tokens * LLM_CACHED_INPUT_USD_PER_MTOK
        + usage.llm_output_tokens * LLM_OUTPUT_USD_PER_MTOK
    ) / 1_000_000
    tts_usd = usage.tts_characters / 1000.0 * TTS_USD_PER_1K_CHARS
    return {
        "stt": {
            "model": SCRIBE_MODEL,
            "audio_seconds": usage.stt_audio_seconds,
            "usd": stt_usd,
        },
        "llm": {
            "model": OPENAI_MODEL,
            "input_tokens": usage.llm_input_tokens,
            "cached_input_tokens": usage.llm_cached_input_tokens,
            "output_tokens": usage.llm_output_tokens,
            "usd": llm_usd,
        },
        "tts": {
            "model": ELEVEN_FLASH_MODEL,
            "characters": usage.tts_characters,
            "usd": tts_usd,
        },
        "total_usd": stt_usd + llm_usd + tts_usd,
    }
