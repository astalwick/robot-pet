"""Session API usage and cost tracking for the voice assistant.

Totals accumulate for the life of the robot-voice process (one robot session)
and reset on restart — we don't persist anything. Prices are the providers'
pay-as-you-go API rates; update the constants below when they change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config.voice import DEFAULT_OPENAI_MODEL
from voice.elevenlabs_io import ELEVEN_FLASH_MODEL, SCRIBE_MODEL


OPENAI_MODEL = DEFAULT_OPENAI_MODEL

# Pay-as-you-go API pricing in USD. Sourced 2026-07-31.
# OpenAI GPT pricing: https://openai.com/api/pricing/  (per 1M tokens)
LLM_PRICE_BY_MODEL = {
    "gpt-5.6-luna": {
        "input": 0.20,
        "cached_input": 0.02,
        "output": 1.20,
    },
    "gpt-5.4-mini": {
        "input": 0.75,
        "cached_input": 0.075,
        "output": 4.50,
    },
    "gpt-5.5": {
        "input": 5.00,
        "cached_input": 0.50,
        "output": 30.00,
    },
}
# ElevenLabs Scribe v2 realtime STT: https://elevenlabs.io/pricing/api
# $0.39 per audio hour -> $0.0065 per audio minute.
STT_USD_PER_MINUTE = 0.0065
# ElevenLabs Flash v2.5 TTS: https://elevenlabs.io/pricing/api  (per 1000 characters)
TTS_USD_PER_1K_CHARS = 0.050


@dataclass
class UsageTotals:
    stt_audio_seconds: float = 0.0
    llm_input_tokens: int = 0
    llm_cached_input_tokens: int = 0
    llm_output_tokens: int = 0
    tts_characters: int = 0
    llm_by_model: dict[str, dict[str, int]] = field(default_factory=dict)


def record_openai_usage(usage: UsageTotals, openai_usage: Any, model: str = DEFAULT_OPENAI_MODEL) -> None:
    """Add token counts from an OpenAI Responses ``response.usage`` object.

    ``input_tokens`` is the full input including the cached portion; we keep the
    cached count separately so the dashboard can price it at the lower rate.
    """
    if openai_usage is None:
        return
    input_tokens = getattr(openai_usage, "input_tokens", 0) or 0
    output_tokens = getattr(openai_usage, "output_tokens", 0) or 0
    details = getattr(openai_usage, "input_tokens_details", None)
    cached_input_tokens = getattr(details, "cached_tokens", 0) or 0
    usage.llm_input_tokens += input_tokens
    usage.llm_output_tokens += output_tokens
    usage.llm_cached_input_tokens += cached_input_tokens
    model_usage = usage.llm_by_model.setdefault(
        model,
        {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
    )
    model_usage["input_tokens"] += input_tokens
    model_usage["cached_input_tokens"] += cached_input_tokens
    model_usage["output_tokens"] += output_tokens


def llm_usage_cost(model: str, input_tokens: int, cached_input_tokens: int, output_tokens: int) -> float:
    prices = LLM_PRICE_BY_MODEL[model]
    uncached_input = max(0, input_tokens - cached_input_tokens)
    return (
        uncached_input * prices["input"]
        + cached_input_tokens * prices["cached_input"]
        + output_tokens * prices["output"]
    ) / 1_000_000


def cost_snapshot(usage: UsageTotals) -> dict[str, Any]:
    """Build the per-category usage + cost block published to the dashboard."""
    stt_usd = usage.stt_audio_seconds / 60.0 * STT_USD_PER_MINUTE
    llm_models = usage.llm_by_model or {
        DEFAULT_OPENAI_MODEL: {
            "input_tokens": usage.llm_input_tokens,
            "cached_input_tokens": usage.llm_cached_input_tokens,
            "output_tokens": usage.llm_output_tokens,
        }
    }
    llm_usd = sum(
        llm_usage_cost(
            model,
            model_usage["input_tokens"],
            model_usage["cached_input_tokens"],
            model_usage["output_tokens"],
        )
        for model, model_usage in llm_models.items()
    )
    tts_usd = usage.tts_characters / 1000.0 * TTS_USD_PER_1K_CHARS
    return {
        "stt": {
            "model": SCRIBE_MODEL,
            "audio_seconds": usage.stt_audio_seconds,
            "usd": stt_usd,
        },
        "llm": {
            "model": ", ".join(llm_models.keys()) if len(llm_models) > 1 else next(iter(llm_models.keys())),
            "input_tokens": usage.llm_input_tokens,
            "cached_input_tokens": usage.llm_cached_input_tokens,
            "output_tokens": usage.llm_output_tokens,
            "usd": llm_usd,
            "models": {
                model: {
                    **model_usage,
                    "usd": llm_usage_cost(
                        model,
                        model_usage["input_tokens"],
                        model_usage["cached_input_tokens"],
                        model_usage["output_tokens"],
                    ),
                }
                for model, model_usage in llm_models.items()
            },
        },
        "tts": {
            "model": ELEVEN_FLASH_MODEL,
            "characters": usage.tts_characters,
            "usd": tts_usd,
        },
        "total_usd": stt_usd + llm_usd + tts_usd,
    }
