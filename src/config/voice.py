"""Persistent config for the robot voice service."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = "/home/pi/.config/robot-pet/voice.json"


class VoiceConfigError(ValueError):
    """Raised when a voice config file exists but cannot be used."""


@dataclass(frozen=True)
class VoiceConfig:
    enabled: bool = False
    input_device: str = "hw:0,0"
    output_device: str = "plughw:0,0"
    sample_rate: int = 16000
    capture_channels: int = 6
    capture_channel_index: int = 1
    output_channels: int = 1
    voice_id: str | None = None
    alternate_voice_id: str | None = None

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
            voice_id=optional_string(values.get("voice_id", defaults.voice_id)),
            alternate_voice_id=optional_string(values.get("alternate_voice_id", defaults.alternate_voice_id)),
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
