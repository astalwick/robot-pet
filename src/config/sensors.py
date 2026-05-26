"""Persistent config for the robot range-sensor service."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from drivers.range import DEFAULT_SENSORS, RangeSensorConfig


DEFAULT_CONFIG_PATH = "/home/pi/.config/robot-pet/sensors.json"

MIN_POLL_RATE_HZ = 0.5
MAX_POLL_RATE_HZ = 20.0

SUPPORTED_KINDS = frozenset({"vl53l0x", "vl53l1x"})


class SensorsConfigError(ValueError):
    """Raised when a sensors config file exists but cannot be used."""


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class SensorEntry:
    name: str
    kind: str
    mux_channel: int


def _default_sensor_entries() -> tuple[SensorEntry, ...]:
    return tuple(
        SensorEntry(name=config.name, kind=config.kind, mux_channel=config.channel)
        for config in DEFAULT_SENSORS
    )


DEFAULT_SENSOR_ENTRIES = _default_sensor_entries()


@dataclass(frozen=True)
class SensorsConfig:
    enabled: bool = True
    poll_rate_hz: float = 10.0
    sensors: tuple[SensorEntry, ...] = DEFAULT_SENSOR_ENTRIES

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "SensorsConfig":
        defaults = cls()
        sensors = _parse_sensors(values.get("sensors"))
        if sensors is None:
            sensors = defaults.sensors
        return cls(
            enabled=bool(values.get("enabled", defaults.enabled)),
            poll_rate_hz=clamp(
                float(values.get("poll_rate_hz", defaults.poll_rate_hz)),
                MIN_POLL_RATE_HZ,
                MAX_POLL_RATE_HZ,
            ),
            sensors=sensors,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "poll_rate_hz": self.poll_rate_hz,
            "sensors": [asdict(entry) for entry in self.sensors],
        }

    def driver_sensors(self) -> list[RangeSensorConfig]:
        return [
            RangeSensorConfig(name=entry.name, kind=entry.kind, channel=entry.mux_channel)
            for entry in self.sensors
        ]


def _parse_sensors(raw: Any) -> tuple[SensorEntry, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise TypeError("sensors must be a list")
    entries: list[SensorEntry] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise TypeError(f"sensors[{index}] must be an object")
        name = item.get("name")
        kind = item.get("kind")
        mux_channel = item.get("mux_channel")
        if not isinstance(name, str) or not name:
            raise TypeError(f"sensors[{index}].name must be a non-empty string")
        if kind not in SUPPORTED_KINDS:
            raise TypeError(f"sensors[{index}].kind must be one of {sorted(SUPPORTED_KINDS)}")
        if not isinstance(mux_channel, int) or mux_channel < 0 or mux_channel > 7:
            raise TypeError(f"sensors[{index}].mux_channel must be an integer from 0 to 7")
        entries.append(SensorEntry(name=name, kind=kind, mux_channel=mux_channel))
    if not entries:
        raise TypeError("sensors must contain at least one entry")
    return tuple(entries)


def load_sensors_config(path: str = DEFAULT_CONFIG_PATH) -> SensorsConfig:
    config_path = Path(path)
    try:
        values = json.loads(config_path.read_text())
    except FileNotFoundError:
        return SensorsConfig()
    except json.JSONDecodeError as exc:
        raise SensorsConfigError(f"Invalid sensors config at {config_path}: {exc}") from exc

    if not isinstance(values, dict):
        raise SensorsConfigError(f"Invalid sensors config at {config_path}: expected a JSON object")

    try:
        return SensorsConfig.from_dict(values)
    except (TypeError, ValueError) as exc:
        raise SensorsConfigError(f"Invalid sensors config at {config_path}: {exc}") from exc


def save_sensors_config(config: SensorsConfig, path: str = DEFAULT_CONFIG_PATH) -> None:
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
