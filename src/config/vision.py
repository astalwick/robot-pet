"""Persistent config for the robot vision service."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = "/home/pi/.config/robot-pet/vision.json"

MIN_DETECTION_RATE_HZ = 0.2
MAX_DETECTION_RATE_HZ = 10.0


class VisionConfigError(ValueError):
    """Raised when a vision config file exists but cannot be used."""


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class VisionConfig:
    enabled: bool = True
    detection_rate_hz: float = 2.0

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "VisionConfig":
        defaults = cls()
        return cls(
            enabled=bool(values.get("enabled", defaults.enabled)),
            detection_rate_hz=clamp(
                float(values.get("detection_rate_hz", defaults.detection_rate_hz)),
                MIN_DETECTION_RATE_HZ,
                MAX_DETECTION_RATE_HZ,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_vision_config(path: str = DEFAULT_CONFIG_PATH) -> VisionConfig:
    config_path = Path(path)
    try:
        values = json.loads(config_path.read_text())
    except FileNotFoundError:
        return VisionConfig()
    except json.JSONDecodeError as exc:
        raise VisionConfigError(f"Invalid vision config at {config_path}: {exc}") from exc

    if not isinstance(values, dict):
        raise VisionConfigError(f"Invalid vision config at {config_path}: expected a JSON object")

    try:
        return VisionConfig.from_dict(values)
    except (TypeError, ValueError) as exc:
        raise VisionConfigError(f"Invalid vision config at {config_path}: {exc}") from exc


def save_vision_config(config: VisionConfig, path: str = DEFAULT_CONFIG_PATH):
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
