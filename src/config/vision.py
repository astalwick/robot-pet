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
MIN_DETECTION_MAX_WIDTH = 160
MAX_DETECTION_MAX_WIDTH = 1280
MIN_HAAR_SCALE_FACTOR = 1.05
MAX_HAAR_SCALE_FACTOR = 1.5
MIN_HAAR_MIN_SIZE = 8
MAX_HAAR_MIN_SIZE = 240


class VisionConfigError(ValueError):
    """Raised when a vision config file exists but cannot be used."""


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class VisionConfig:
    enabled: bool = True
    detection_rate_hz: float = 2.0
    detection_max_width: int = 640
    haar_scale_factor: float = 1.1
    haar_min_size: int = 24

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
            detection_max_width=int(
                clamp(
                    float(values.get("detection_max_width", defaults.detection_max_width)),
                    MIN_DETECTION_MAX_WIDTH,
                    MAX_DETECTION_MAX_WIDTH,
                )
            ),
            haar_scale_factor=clamp(
                float(values.get("haar_scale_factor", defaults.haar_scale_factor)),
                MIN_HAAR_SCALE_FACTOR,
                MAX_HAAR_SCALE_FACTOR,
            ),
            haar_min_size=int(
                clamp(
                    float(values.get("haar_min_size", defaults.haar_min_size)),
                    MIN_HAAR_MIN_SIZE,
                    MAX_HAAR_MIN_SIZE,
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


VISION_FIELDS = (
    {
        "key": "enabled",
        "label": "Vision enabled",
        "type": "boolean",
        "help": "Run face detection on the camera feed",
    },
    {
        "key": "detection_rate_hz",
        "label": "Detection rate (Hz)",
        "type": "number",
        "help": "0.2 .. 10.0",
        "min": 0.2,
        "max": 10.0,
        "step": 0.1,
    },
    {
        "key": "detection_max_width",
        "label": "Detector max width",
        "type": "number",
        "help": "160 .. 1280; current default is 640",
        "min": 160,
        "max": 1280,
        "step": 20,
    },
    {
        "key": "haar_scale_factor",
        "label": "Haar scale factor",
        "type": "number",
        "help": "1.05 .. 1.50; higher is faster but may miss faces",
        "min": 1.05,
        "max": 1.5,
        "step": 0.05,
    },
    {
        "key": "haar_min_size",
        "label": "Haar min face size",
        "type": "number",
        "help": "8 .. 240 px; higher ignores small/far faces",
        "min": 8,
        "max": 240,
        "step": 1,
    },
)


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
