"""Persistent drive tuning for gamepad teleop."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = "/home/pi/.config/robot-pet/teleop.json"
DEFAULT_QPPS = 2425


class DriveTuningConfigError(ValueError):
    """Raised when a drive tuning config file exists but cannot be used."""


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class DriveTuning:
    speed_scale: float = 0.25
    turbo_scale: float = 0.75
    turn_scale: float = 1.0
    left_stick_deadzone: float = 0.15
    right_stick_deadzone: float = 0.15
    qpps_slew_limit: float = 5000.0

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "DriveTuning":
        defaults = cls()
        return cls(
            speed_scale=clamp(float(values.get("speed_scale", defaults.speed_scale)), 0.0, 1.0),
            turbo_scale=clamp(float(values.get("turbo_scale", defaults.turbo_scale)), 0.0, 1.0),
            turn_scale=clamp(float(values.get("turn_scale", defaults.turn_scale)), 0.0, 1.0),
            left_stick_deadzone=clamp(
                float(values.get("left_stick_deadzone", defaults.left_stick_deadzone)),
                0.0,
                1.0,
            ),
            right_stick_deadzone=clamp(
                float(values.get("right_stick_deadzone", defaults.right_stick_deadzone)),
                0.0,
                1.0,
            ),
            qpps_slew_limit=clamp(float(values.get("qpps_slew_limit", defaults.qpps_slew_limit)), 100.0, 50000.0),
        )

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


TUNING_FIELDS = (
    ("speed_scale", "Normal speed", "0.25 = 25%"),
    ("turbo_scale", "Turbo speed", "0.75 = 75%"),
    ("turn_scale", "Turn scale", "1.0 = full turn"),
    ("left_stick_deadzone", "Left stick deadzone", "0.15"),
    ("right_stick_deadzone", "Right stick deadzone", "0.15"),
    ("qpps_slew_limit", "QPPS slew", "encoder counts/sec/sec"),
)


def load_drive_tuning(path: str = DEFAULT_CONFIG_PATH) -> DriveTuning:
    config_path = Path(path)
    try:
        values = json.loads(config_path.read_text())
    except FileNotFoundError:
        return DriveTuning()
    except json.JSONDecodeError as exc:
        raise DriveTuningConfigError(f"Invalid drive tuning config at {config_path}: {exc}") from exc

    if not isinstance(values, dict):
        raise DriveTuningConfigError(f"Invalid drive tuning config at {config_path}: expected a JSON object")

    try:
        return DriveTuning.from_dict(values)
    except (TypeError, ValueError) as exc:
        raise DriveTuningConfigError(f"Invalid drive tuning config at {config_path}: {exc}") from exc


def save_drive_tuning(tuning: DriveTuning, path: str = DEFAULT_CONFIG_PATH):
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(tuning.to_dict(), indent=2, sort_keys=True) + "\n"

    with tempfile.NamedTemporaryFile(
        "w",
        dir=config_path.parent,
        prefix=f".{config_path.name}.",
        delete=False,
    ) as file_obj:
        file_obj.write(data)
        temp_name = file_obj.name

    os.replace(temp_name, config_path)
