"""Persistent config for the robot range-sensor service."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from drivers.imu import BNO085Config
from drivers.range import DEFAULT_SENSORS, RangeSensorConfig


DEFAULT_CONFIG_PATH = "/home/pi/.config/robot-pet/sensors.json"

MIN_POLL_RATE_HZ = 0.5
MAX_POLL_RATE_HZ = 20.0

SUPPORTED_KINDS = frozenset({"vl53l0x", "vl53l1x"})
SENSOR_ROLES = frozenset({"cliff", "forward"})


class SensorsConfigError(ValueError):
    """Raised when a sensors config file exists but cannot be used."""


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class SafetyConfig:
    """Motion gating thresholds for robot-motion (Phase 3)."""

    enabled: bool = False
    cliff_trip_above_mm: int = 200
    forward_stop_below_mm: int = 150


@dataclass(frozen=True)
class SensorEntry:
    name: str
    kind: str
    mux_channel: int
    role: str | None = None
    trip_above_mm: int | None = None
    stop_below_mm: int | None = None
    offset_mm: int = 0


@dataclass(frozen=True)
class ImuEntry:
    enabled: bool = False
    kind: str = "bno085"
    mux_channel: int = 5
    address: int = 0x4A
    mode: str = "game"
    zero_quaternion: tuple[float, float, float, float] | None = None
    zero_gravity: tuple[float, float, float] | None = None


def _default_sensor_entries() -> tuple[SensorEntry, ...]:
    return tuple(
        SensorEntry(
            name=config.name,
            kind=config.kind,
            mux_channel=config.channel,
            role="cliff",
        )
        for config in DEFAULT_SENSORS
    )


DEFAULT_SENSOR_ENTRIES = _default_sensor_entries()


@dataclass(frozen=True)
class SensorsConfig:
    enabled: bool = True
    poll_rate_hz: float = 10.0
    safety: SafetyConfig = SafetyConfig()
    imu: ImuEntry = ImuEntry()
    sensors: tuple[SensorEntry, ...] = DEFAULT_SENSOR_ENTRIES

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "SensorsConfig":
        defaults = cls()
        sensors = _parse_sensors(values.get("sensors"), defaults.safety)
        if sensors is None:
            sensors = defaults.sensors
        return cls(
            enabled=bool(values.get("enabled", defaults.enabled)),
            poll_rate_hz=clamp(
                float(values.get("poll_rate_hz", defaults.poll_rate_hz)),
                MIN_POLL_RATE_HZ,
                MAX_POLL_RATE_HZ,
            ),
            safety=_parse_safety(values.get("safety")),
            imu=_parse_imu(values.get("imu")),
            sensors=sensors,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "poll_rate_hz": self.poll_rate_hz,
            "safety": asdict(self.safety),
            "imu": _imu_entry_to_dict(self.imu),
            "sensors": [_sensor_entry_to_dict(entry) for entry in self.sensors],
        }

    def driver_sensors(self) -> list[RangeSensorConfig]:
        return [
            RangeSensorConfig(
                name=entry.name,
                kind=entry.kind,
                channel=entry.mux_channel,
                offset_mm=entry.offset_mm,
            )
            for entry in self.sensors
        ]

    def driver_imu(self) -> BNO085Config | None:
        if not self.imu.enabled:
            return None
        if self.imu.zero_quaternion is None or self.imu.zero_gravity is None:
            return None
        return BNO085Config(
            channel=self.imu.mux_channel,
            address=self.imu.address,
            mode=self.imu.mode,
            zero_quaternion=self.imu.zero_quaternion,
            zero_gravity=self.imu.zero_gravity,
        )


def _parse_safety(raw: Any) -> SafetyConfig:
    defaults = SafetyConfig()
    if raw is None:
        return defaults
    if not isinstance(raw, dict):
        raise TypeError("safety must be an object")
    return SafetyConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        cliff_trip_above_mm=_parse_positive_mm(
            raw.get("cliff_trip_above_mm", defaults.cliff_trip_above_mm),
            "safety.cliff_trip_above_mm",
        ),
        forward_stop_below_mm=_parse_positive_mm(
            raw.get("forward_stop_below_mm", defaults.forward_stop_below_mm),
            "safety.forward_stop_below_mm",
        ),
    )


def _parse_positive_mm(value: Any, field: str) -> int:
    # bool is an int subclass; without this check `true` parses as 1 mm.
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise TypeError(f"{field} must be a positive integer")
    return value


def _sensor_entry_to_dict(entry: SensorEntry) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": entry.name,
        "kind": entry.kind,
        "mux_channel": entry.mux_channel,
    }
    if entry.role is not None:
        data["role"] = entry.role
    if entry.trip_above_mm is not None:
        data["trip_above_mm"] = entry.trip_above_mm
    if entry.stop_below_mm is not None:
        data["stop_below_mm"] = entry.stop_below_mm
    if entry.offset_mm:
        data["offset_mm"] = entry.offset_mm
    return data


def _imu_entry_to_dict(entry: ImuEntry) -> dict[str, Any]:
    data: dict[str, Any] = {
        "enabled": entry.enabled,
        "kind": entry.kind,
        "mux_channel": entry.mux_channel,
        "address": f"0x{entry.address:02x}",
        "mode": entry.mode,
    }
    if entry.zero_quaternion is not None:
        data["zero_quaternion"] = list(entry.zero_quaternion)
    if entry.zero_gravity is not None:
        data["zero_gravity"] = list(entry.zero_gravity)
    return data


def _parse_imu(raw: Any) -> ImuEntry:
    if raw is None:
        return ImuEntry()
    if not isinstance(raw, dict):
        raise TypeError("imu must be an object")
    kind = raw.get("kind", "bno085")
    if kind != "bno085":
        raise TypeError("imu.kind must be bno085")
    mux_channel = raw.get("mux_channel", 5)
    if not isinstance(mux_channel, int) or mux_channel < 0 or mux_channel > 7:
        raise TypeError("imu.mux_channel must be an integer from 0 to 7")
    mode = raw.get("mode", "game")
    if mode not in ("game", "rotation"):
        raise TypeError("imu.mode must be game or rotation")
    return ImuEntry(
        enabled=bool(raw.get("enabled", False)),
        kind=kind,
        mux_channel=mux_channel,
        address=_parse_imu_address(raw.get("address", 0x4A)),
        mode=mode,
        zero_quaternion=_parse_float_tuple(
            raw.get("zero_quaternion"), 4, "imu.zero_quaternion"
        ),
        zero_gravity=_parse_float_tuple(raw.get("zero_gravity"), 3, "imu.zero_gravity"),
    )


def _parse_imu_address(value: Any) -> int:
    if isinstance(value, str):
        value = int(value, 0)
    if not isinstance(value, int) or value not in (0x4A, 0x4B):
        raise TypeError("imu.address must be 0x4a or 0x4b")
    return value


def _parse_float_tuple(raw: Any, length: int, field: str) -> tuple[float, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) != length:
        raise TypeError(f"{field} must be a list of {length} numbers")
    if not all(isinstance(value, int | float) for value in raw):
        raise TypeError(f"{field} must be a list of {length} numbers")
    return tuple(float(value) for value in raw)


def cliff_trip_mm(entry: SensorEntry, safety: SafetyConfig) -> int | None:
    if entry.role != "cliff":
        return None
    return entry.trip_above_mm if entry.trip_above_mm is not None else safety.cliff_trip_above_mm


def forward_stop_mm(entry: SensorEntry, safety: SafetyConfig) -> int | None:
    if entry.role != "forward":
        return None
    return entry.stop_below_mm if entry.stop_below_mm is not None else safety.forward_stop_below_mm


def _parse_sensors(raw: Any, safety: SafetyConfig) -> tuple[SensorEntry, ...] | None:
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
        role = item.get("role")
        if role is not None and role not in SENSOR_ROLES:
            raise TypeError(f"sensors[{index}].role must be one of {sorted(SENSOR_ROLES)}")
        trip_above_mm = item.get("trip_above_mm")
        stop_below_mm = item.get("stop_below_mm")
        offset_mm = item.get("offset_mm", 0)
        if not isinstance(offset_mm, int) or isinstance(offset_mm, bool):
            raise TypeError(f"sensors[{index}].offset_mm must be an integer")
        if trip_above_mm is not None:
            trip_above_mm = _parse_positive_mm(trip_above_mm, f"sensors[{index}].trip_above_mm")
        if stop_below_mm is not None:
            stop_below_mm = _parse_positive_mm(stop_below_mm, f"sensors[{index}].stop_below_mm")
        if role == "cliff" and stop_below_mm is not None:
            raise TypeError(f"sensors[{index}].stop_below_mm is only valid for forward sensors")
        if role == "forward" and trip_above_mm is not None:
            raise TypeError(f"sensors[{index}].trip_above_mm is only valid for cliff sensors")
        if role is None and (trip_above_mm is not None or stop_below_mm is not None):
            raise TypeError(f"sensors[{index}] distance overrides require role")
        entries.append(
            SensorEntry(
                name=name,
                kind=kind,
                mux_channel=mux_channel,
                role=role,
                trip_above_mm=trip_above_mm,
                stop_below_mm=stop_below_mm,
                offset_mm=offset_mm,
            )
        )
    if not entries:
        raise TypeError("sensors must contain at least one entry")
    return tuple(entries)


SENSORS_FIELDS = (
    {
        "key": "enabled",
        "label": "Poll sensors",
        "type": "boolean",
        "help": "robot-sensors reads ToF hardware and publishes telemetry",
    },
    {
        "key": "poll_rate_hz",
        "label": "Poll rate (Hz)",
        "type": "number",
        "help": "0.5 .. 20.0",
        "min": 0.5,
        "max": 20.0,
        "step": 0.5,
    },
    {
        "key": "safety_enabled",
        "label": "Safety gating",
        "type": "boolean",
        "help": "robot-motion blocks forward drive when cliff or forward rules trip",
    },
    {
        "key": "cliff_trip_above_mm",
        "label": "Cliff trip above (mm)",
        "type": "number",
        "help": "Cliff sensor trips when distance is above this (floor gone / out of range)",
        "min": 1,
        "max": 4000,
        "step": 1,
    },
    {
        "key": "forward_stop_below_mm",
        "label": "Forward stop below (mm)",
        "type": "number",
        "help": "Forward sensor trips when distance is below this (obstacle too close)",
        "min": 1,
        "max": 4000,
        "step": 1,
    },
)


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
