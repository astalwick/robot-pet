"""Persistent config for the robot voice service."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = "/home/pi/.config/robot-pet/voice.json"
DEFAULT_WAKE_MODEL_PATH = "/home/pi/robot-pet/models/wake/Hey_Bloop.onnx"
DEFAULT_WAKE_CHIME_PATH = "/home/pi/robot-pet/assets/audio/wake_chime.wav"
DEFAULT_SESSION_END_CHIME_PATH = "/home/pi/robot-pet/assets/audio/session_end_chime.wav"
DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
OPENAI_MODEL_CHOICES = ("gpt-5.4-mini", "gpt-5.5")
MIN_AUDIO_GAIN = 0.0
MAX_AUDIO_GAIN = 3.0


class VoiceConfigError(ValueError):
    """Raised when a voice config file exists but cannot be used."""


@dataclass(frozen=True)
class VoiceConfig:
    enabled: bool = False
    input_device: str = "XVF3800"
    output_device: str = "XVF3800"
    sample_rate: int = 16000
    capture_channels: int = 6
    capture_channel_index: int = 1
    output_channels: int = 1
    input_gain: float = 1.0
    output_gain: float = 1.0
    voice_id: str | None = None
    alternate_voice_id: str | None = None
    personality: str = "default"
    openai_model: str = DEFAULT_OPENAI_MODEL
    speculative_playback_enabled: bool = False
    barge_in_enabled: bool = True
    barge_in_min_words: int = 3
    barge_in_min_chars: int = 12
    barge_in_cooldown_secs: float = 0.35
    barge_in_min_rms: int = 700
    barge_in_sustain_ms: int = 350
    barge_in_explicit_interrupts: str = "stop,wait,no,cancel,pause"
    barge_in_explicit_requires_sustain: bool = False
    assistant_echo_enabled: bool = False
    assistant_echo_similarity: float = 0.9
    wake_word_enabled: bool = False
    wake_word_model_path: str = DEFAULT_WAKE_MODEL_PATH
    wake_threshold: float = 0.5
    wake_debounce_secs: float = 2.0
    wake_chime_path: str = DEFAULT_WAKE_CHIME_PATH
    session_end_chime_path: str = DEFAULT_SESSION_END_CHIME_PATH
    session_idle_secs: float = 30.0

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "VoiceConfig":
        defaults = cls()
        config = cls(
            enabled=bool(values.get("enabled", defaults.enabled)),
            input_device=str(values.get("input_device", defaults.input_device)),
            output_device=str(values.get("output_device", defaults.output_device)),
            sample_rate=int(values.get("sample_rate", defaults.sample_rate)),
            capture_channels=int(values.get("capture_channels", defaults.capture_channels)),
            capture_channel_index=int(values.get("capture_channel_index", defaults.capture_channel_index)),
            output_channels=int(values.get("output_channels", defaults.output_channels)),
            input_gain=clamp(float(values.get("input_gain", defaults.input_gain)), MIN_AUDIO_GAIN, MAX_AUDIO_GAIN),
            output_gain=clamp(float(values.get("output_gain", defaults.output_gain)), MIN_AUDIO_GAIN, MAX_AUDIO_GAIN),
            voice_id=optional_string(values.get("voice_id", defaults.voice_id)),
            alternate_voice_id=optional_string(values.get("alternate_voice_id", defaults.alternate_voice_id)),
            personality=str(values.get("personality", defaults.personality)).strip() or defaults.personality,
            openai_model=str(values.get("openai_model", defaults.openai_model)).strip() or defaults.openai_model,
            speculative_playback_enabled=bool(
                values.get("speculative_playback_enabled", defaults.speculative_playback_enabled)
            ),
            barge_in_enabled=bool(values.get("barge_in_enabled", defaults.barge_in_enabled)),
            barge_in_min_words=int(values.get("barge_in_min_words", defaults.barge_in_min_words)),
            barge_in_min_chars=int(values.get("barge_in_min_chars", defaults.barge_in_min_chars)),
            barge_in_cooldown_secs=float(values.get("barge_in_cooldown_secs", defaults.barge_in_cooldown_secs)),
            barge_in_min_rms=int(values.get("barge_in_min_rms", defaults.barge_in_min_rms)),
            barge_in_sustain_ms=int(values.get("barge_in_sustain_ms", defaults.barge_in_sustain_ms)),
            barge_in_explicit_interrupts=str(
                values.get("barge_in_explicit_interrupts", defaults.barge_in_explicit_interrupts)
            ),
            barge_in_explicit_requires_sustain=bool(
                values.get("barge_in_explicit_requires_sustain", defaults.barge_in_explicit_requires_sustain)
            ),
            assistant_echo_enabled=bool(
                values.get("assistant_echo_enabled", defaults.assistant_echo_enabled)
            ),
            assistant_echo_similarity=float(
                values.get("assistant_echo_similarity", defaults.assistant_echo_similarity)
            ),
            wake_word_enabled=bool(values.get("wake_word_enabled", defaults.wake_word_enabled)),
            wake_word_model_path=str(values.get("wake_word_model_path", defaults.wake_word_model_path)),
            wake_threshold=float(values.get("wake_threshold", defaults.wake_threshold)),
            wake_debounce_secs=float(values.get("wake_debounce_secs", defaults.wake_debounce_secs)),
            wake_chime_path=str(values.get("wake_chime_path", defaults.wake_chime_path)),
            session_end_chime_path=str(
                values.get("session_end_chime_path", defaults.session_end_chime_path)
            ),
            session_idle_secs=float(values.get("session_idle_secs", defaults.session_idle_secs)),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.sample_rate != 16000:
            raise VoiceConfigError("sample_rate must be 16000")
        if self.capture_channels != 6:
            raise VoiceConfigError("capture_channels must be 6")
        if self.capture_channel_index < 0 or self.capture_channel_index >= self.capture_channels:
            raise VoiceConfigError("capture_channel_index must be between 0 and 5")
        if self.output_channels != 1:
            raise VoiceConfigError("output_channels must be 1")
        if self.openai_model not in OPENAI_MODEL_CHOICES:
            raise VoiceConfigError(f"openai_model must be one of: {', '.join(OPENAI_MODEL_CHOICES)}")
        if self.wake_threshold < 0.0 or self.wake_threshold > 1.0:
            raise VoiceConfigError("wake_threshold must be between 0 and 1")
        if self.wake_debounce_secs < 0.0:
            raise VoiceConfigError("wake_debounce_secs must be >= 0")
        if self.session_idle_secs < 0.0:
            raise VoiceConfigError("session_idle_secs must be >= 0")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


VOICE_FIELDS = (
    {
        "key": "wake_word_enabled",
        "label": "Voice on",
        "type": "boolean",
        "help": "Hey Bloop wakes the assistant. Talk now starts active listening.",
    },
    {
        "key": "session_idle_secs",
        "label": "Session idle (s)",
        "type": "number",
        "help": "0 disables. After user and robot speech are quiet, return to wake listening.",
        "min": 0.0,
        "max": 600.0,
        "step": 5.0,
    },
    {
        "key": "wake_threshold",
        "label": "Wake threshold",
        "type": "number",
        "help": "0.0 .. 1.0 openWakeWord score to fire",
        "min": 0.0,
        "max": 1.0,
        "step": 0.05,
    },
    {
        "key": "wake_debounce_secs",
        "label": "Wake debounce (s)",
        "type": "number",
        "help": "0.0 .. 10.0 minimum seconds between wake chimes",
        "min": 0.0,
        "max": 10.0,
        "step": 0.5,
    },
    {
        "key": "wake_word_model_path",
        "label": "Wake model path",
        "type": "text",
        "help": "ONNX model on the Pi, e.g. /home/pi/robot-pet/models/wake/Hey_Bloop.onnx",
    },
    {
        "key": "wake_chime_path",
        "label": "Wake chime path",
        "type": "text",
        "help": "16 kHz mono WAV played on wake",
    },
    {
        "key": "input_device",
        "label": "Input device",
        "type": "text",
        "help": "ALSA capture device, e.g. hw:0,0",
    },
    {
        "key": "output_device",
        "label": "Output device",
        "type": "text",
        "help": "ALSA playback device, e.g. plughw:0,0",
    },
    {
        "key": "capture_channel_index",
        "label": "Capture channel",
        "type": "number",
        "help": "0 .. 5",
        "min": 0,
        "max": 5,
        "step": 1,
    },
    {
        "key": "input_gain",
        "label": "Mic gain",
        "type": "number",
        "help": "0.0 mute, 1.0 normal, up to 3.0",
        "min": 0.0,
        "max": 3.0,
        "step": 0.1,
    },
    {
        "key": "output_gain",
        "label": "Speaker volume",
        "type": "number",
        "help": "0.0 mute, 1.0 normal, up to 3.0",
        "min": 0.0,
        "max": 3.0,
        "step": 0.1,
    },
    {
        "key": "openai_model",
        "label": "OpenAI model",
        "type": "select",
        "options": list(OPENAI_MODEL_CHOICES),
        "help": "Conversation model. Requests use reasoning effort none.",
    },
    {
        "key": "speculative_playback_enabled",
        "label": "Speculative speech",
        "type": "boolean",
        "help": "Let stable partial transcripts speak before Scribe commits",
    },
    {
        "key": "barge_in_enabled",
        "label": "Barge-in enabled",
        "type": "boolean",
        "help": "Allow interrupting the assistant while it is speaking",
    },
    {
        "key": "barge_in_min_rms",
        "label": "Barge-in min RMS",
        "type": "number",
        "help": "100 .. 5000",
        "min": 100,
        "max": 5000,
        "step": 50,
    },
    {
        "key": "barge_in_sustain_ms",
        "label": "Barge-in sustain (ms)",
        "type": "number",
        "help": "0 .. 1500",
        "min": 0,
        "max": 1500,
        "step": 50,
    },
    {
        "key": "barge_in_cooldown_secs",
        "label": "Barge-in cooldown (s)",
        "type": "number",
        "help": "0.0 .. 2.0",
        "min": 0.0,
        "max": 2.0,
        "step": 0.05,
    },
    {
        "key": "barge_in_min_words",
        "label": "Barge-in min words",
        "type": "number",
        "help": "1 .. 10",
        "min": 1,
        "max": 10,
        "step": 1,
    },
    {
        "key": "barge_in_min_chars",
        "label": "Barge-in min chars",
        "type": "number",
        "help": "1 .. 80",
        "min": 1,
        "max": 80,
        "step": 1,
    },
    {
        "key": "barge_in_explicit_interrupts",
        "label": "Explicit interrupts",
        "type": "text",
        "help": "Comma-separated words, e.g. stop,wait,no",
    },
    {
        "key": "barge_in_explicit_requires_sustain",
        "label": "Explicit needs sustain",
        "type": "boolean",
        "help": "Explicit interrupt words also require sustained near-end audio",
    },
    {
        "key": "assistant_echo_enabled",
        "label": "Echo suppression",
        "type": "boolean",
        "help": "Software echo suppression (off by default — rely on XVF3800 hardware AEC)",
    },
    {
        "key": "assistant_echo_similarity",
        "label": "Echo similarity",
        "type": "number",
        "help": "0.5 .. 1.0 (only used when echo suppression is on)",
        "min": 0.5,
        "max": 1.0,
        "step": 0.05,
    },
)


def load_voice_config(path: str = DEFAULT_CONFIG_PATH) -> VoiceConfig:
    config_path = Path(path)
    try:
        values = json.loads(config_path.read_text())
    except FileNotFoundError:
        return VoiceConfig()
    except json.JSONDecodeError as exc:
        raise VoiceConfigError(f"Invalid voice config at {config_path}: {exc}") from exc

    if not isinstance(values, dict):
        raise VoiceConfigError(f"Invalid voice config at {config_path}: expected a JSON object")

    try:
        return VoiceConfig.from_dict(values)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, VoiceConfigError):
            raise
        raise VoiceConfigError(f"Invalid voice config at {config_path}: {exc}") from exc


def save_voice_config(config: VoiceConfig, path: str = DEFAULT_CONFIG_PATH):
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n"

    with tempfile.NamedTemporaryFile(
        "w",
        dir=config_path.parent,
        prefix=f".{config_path.name}.",
        delete=False,
    ) as file_obj:
        file_obj.write(data)
        temp_name = file_obj.name

    os.replace(temp_name, config_path)
