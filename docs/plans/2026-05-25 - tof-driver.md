# Range Sensor Driver Plan (VL53L0X/VL53L1X + TCA9548A)

## Goal

Add a framework-agnostic driver for range sensors behind the Grove TCA9548A I2C mux, following [ARCHITECTURE.md](../ARCHITECTURE.md).

Hardware is validated: mux at **0x70**, three VL53L0X sensors at **0x29** on mux channels **0, 1, 2** (Grove ports 1–3). Bring-up script: `scripts/diagnostics/i2c-tof-scan.py`.

Body Phase 2 ([body-phase-2.md](../phases/body-phase-2.md)) eventually needs cliff/obstacle sensing, safety gating in `robot-motion`, and telemetry. This plan covers the driver and diagnostics first; services come in later phases.

## Guiding Principle

**Drivers are framework-agnostic. Services are throwaway scaffolding.**

The driver owns I2C, mux setup, sensor init, and distance reads. It does not know about systemd, telemetry sockets, safety thresholds, or cliff vs forward policy.

## Current Hardware

| Piece | Detail |
|-------|--------|
| Mux | TCA9548A @ **0x70**, channel select = `1 << n` |
| Sensors now | 3× VL53L0X @ **0x29**, mux channels **0, 1, 2** |
| Sensors later | 3× VL53L1X forward sensors, likely mux channels **3, 4, 5** |
| Bring-up | `scripts/diagnostics/i2c-tof-scan.py` (presence + chip ID) |

The three VL53L0X sensors on the bench are **cliff** sensors. Phase 2 also calls for **VL53L1X** forward sensors. Support that known second chip with a simple `kind` switch, but do not build a generic sensor plugin system.

## Layering

```
┌─────────────────────────────────────────────────────────┐
│  robot-motion (later)     safety rules, stop/refuse     │
│  robot-sensors (later)    poll loop → telemetry         │
├─────────────────────────────────────────────────────────┤
│  src/config/sensors.py    logical sensor config         │
├─────────────────────────────────────────────────────────┤
│  src/drivers/range.py     mux + VL53L0X/VL53L1X ranges  │  ← build first
├─────────────────────────────────────────────────────────┤
│  scripts/diagnostics/     smoke tests, no business logic│
└─────────────────────────────────────────────────────────┘
```

### Driver responsibilities

- Open one I2C bus on the Pi and one `TCA9548A` mux object.
- Init each configured range sensor once at startup.
- Read distance in mm from each named sensor.
- `cleanup()` — stop ranging, release bus (mirror `MotorDriver.cleanup()`).

### Driver does not

- Import ROS, systemd, or telemetry modules.
- Implement cliff vs obstacle policy or safety thresholds.

### Services (later, Body Phase 2)

- `robot-sensors`: owns `RangeDriver`, polls at N Hz, publishes JSON via existing telemetry helpers.
- `robot-motion`: reads latest sensor snapshot; applies stop/refuse rules. Owns `MotorDriver`; `gamepad-teleop` publishes intents instead of driving RoboClaw directly.

## File Layout

| File | Purpose |
|------|---------|
| `src/drivers/range.py` | `RangeDriver` + Adafruit mux/range sensor setup |
| `src/config/sensors.py` | `sensors.json`: logical names → kind + mux channel |
| `tests/test_range.py` | Fake mux + fake sensor backends (no hardware) |
| `scripts/diagnostics/i2c-tof-range.py` | Live mm readings |

Keep chip-ID probe logic in `i2c-tof-scan.py`; duplicate small probe snippets in the driver only if needed.

## `RangeDriver` API

Follow `MotorDriver` / `CameraDriver` patterns: constructor args + optional factory for tests.

```python
@dataclass(frozen=True)
class RangeSensorConfig:
    name: str           # e.g. "cliff_left"
    kind: str           # "vl53l0x" or "vl53l1x"
    channel: int

@dataclass(frozen=True)
class RangeReading:
    name: str           # e.g. "cliff_left"
    kind: str
    channel: int
    distance_mm: int | None   # None = out of range / failed read
    ok: bool

class RangeDriver:
    def __init__(
        self,
        sensors: list[RangeSensorConfig],
        mux_address: int = 0x70,
        range_address: int = 0x29,
        i2c_factory: Callable[[], Any] | None = None,
        mux_factory: Callable[..., Any] | None = None,
        vl53l0x_factory: Callable[..., Any] | None = None,
        vl53l1x_factory: Callable[..., Any] | None = None,
    ): ...

    def read(self, name: str) -> RangeReading: ...
    def read_all(self) -> list[RangeReading]: ...
    def cleanup(self) -> None: ...
```

**Bus access:** one `threading.Lock` around sensor reads so a future `robot-sensors` thread does not interleave transactions inside the same process. Diagnostics should be run while any future sensor service is stopped.

**Default sensor map** (until mounts are final):

```python
DEFAULT_SENSORS = [
    RangeSensorConfig("cliff_left", "vl53l0x", 0),
    RangeSensorConfig("cliff_center", "vl53l0x", 1),
    RangeSensorConfig("cliff_right", "vl53l0x", 2),
]
```

Config file overrides this without code changes when sensors are remounted or VL53L1X forward sensors are added.

## Ranging Library

Use the maintained Adafruit CircuitPython stack through Blinka:

**Recommendation:**

1. **Runtime dependency (Linux only):** install `adafruit-blinka`, `adafruit-circuitpython-tca9548a`, `adafruit-circuitpython-vl53l0x`, and later `adafruit-circuitpython-vl53l1x` in `setup.sh` on the Pi.
2. **Driver wraps the mux object:** `adafruit_tca9548a.TCA9548A(i2c, address=0x70)` exposes `mux[channel]` as the I2C bus for each sensor.
3. **Chip-specific factories:** `vl53l0x_factory(mux[channel])` for cliff sensors now; `vl53l1x_factory(mux[channel])` for forward sensors later.
4. **Factory injection in tests:** return fakes with the same distance property/method shape so Mac CI never needs Blinka or hardware.

Avoid the older `VL53L0X` PyPI/Pimoroni C-wrapper path unless Adafruit fails on the Pi. The older wrappers have API/version drift and more Pi 5 / Python 3.11 risk.

**VL53L1X (forward, later):** add the second factory branch when those sensors arrive. Keep BNO085 as a separate `src/drivers/imu.py`; it is not a range sensor.

## Phase 1 Implementation Notes

Keep the first implementation boring and direct:

- For real hardware, create I2C with `board.I2C()` or `busio.I2C(board.SCL, board.SDA)`.
- Create the mux with `adafruit_tca9548a.TCA9548A(i2c, address=mux_address)`.
- For a VL53L0X sensor, instantiate `adafruit_vl53l0x.VL53L0X(mux[channel])`.
- Read distance from the sensor's `.range` property. Treat exceptions from that property as a failed read for that one sensor.
- `cleanup()` can be a no-op at first unless the Adafruit objects expose a useful close/deinit method.
- The first live diagnostic should hard-code the current three VL53L0X sensors on channels 0, 1, and 2. Once that prints range values, wrap the same pattern in `RangeDriver`.
- Do not manually write mux channel masks in the driver. Let the Adafruit mux channel object own that.
- Do not build a plugin system. Use a small `if kind == "vl53l0x"` / `elif kind == "vl53l1x"` branch when VL53L1X support is added.

## Config (`src/config/sensors.py`)

Same shape as `vision.py` / `teleop.py`:

- Path: `/home/pi/.config/robot-pet/sensors.json`
- Fields: `enabled`, `poll_rate_hz`, `sensors: [{ "name", "kind", "mux_channel" }]`
- Driver constructor accepts plain dataclasses; services load config at the boundary so tests do not touch the filesystem.

No safety thresholds in v1 config — those belong in `robot-motion` when it exists.

**Suggested default for the current robot:**

```json
{
  "enabled": true,
  "poll_rate_hz": 10,
  "sensors": [
    { "name": "cliff_left", "kind": "vl53l0x", "mux_channel": 0 },
    { "name": "cliff_center", "kind": "vl53l0x", "mux_channel": 1 },
    { "name": "cliff_right", "kind": "vl53l0x", "mux_channel": 2 }
  ]
}
```

## OS / Setup (one-time)

Add to **`setup.sh`** (not `redeploy-robot.sh`):

1. Enable I2C if not already (`dtparam=i2c_arm=on` or raspi-config non-interactive).
2. Add user to `i2c` group.
3. Optional when more devices are added: `dtparam=i2c_baudrate=400000` in `/boot/firmware/config.txt` (see [robot-build-gotchas.md](../phases/robot-build-gotchas.md)).
4. Pi-only pip install of the Adafruit Blinka, TCA9548A, and VL53L0X packages.

`redeploy-robot.sh` stays pull + reinstall + restart — no hardware mutation.

`i2c-tools` is already installed in `setup.sh`. Mux channel selects from CLI tests are volatile and do not need to be replayed on deploy.

## Testing

| Test | How |
|------|-----|
| Unit | Fake mux records requested channels; fake sensors return fixed mm |
| Unit | `read_all` order follows configured sensor order |
| Unit | OSError on one channel → that sensor `ok=False`, others still read |
| Hardware | `i2c-tof-range.py` prints mm at ~5 Hz for all three |
| Hardware | Wave hand / floor drop — sanity only |

No tests that require real I2C in CI.

## Delivery Phases

### Phase 1 — Driver + diagnostics (now)

- `src/drivers/range.py`
- `tests/test_range.py`
- `scripts/diagnostics/i2c-tof-range.py`
- `setup.sh`: I2C group + Adafruit Blinka/TCA9548A/VL53L0X packages on Pi
- Document optional Linux extra in `pyproject.toml` or install only via `setup.sh` (keeps Mac venv clean)

### Phase 2 — Observability

- `src/config/sensors.py` + example `sensors.json`
- `robot-sensors` service stub: poll `read_all()`, publish via `telemetry/messages.py`
- Dashboard row only when there is real data (per TUI decisions: avoid placeholder UI)
- Add VL53L1X forward sensors by adding `kind: "vl53l1x"` entries and the VL53L1X package/factory branch.

### Phase 3 — Safety (Body Phase 2)

- `robot-motion` owns `MotorDriver` + reads sensor snapshot
- Simple rules: any cliff below threshold → refuse forward / command stop
- `gamepad-teleop` publishes intents to motion instead of driving RoboClaw directly

Range driver unchanged in Phase 3.

## ROS2 Migration

Future `sensors_node.py` imports `RangeDriver`, publishes `sensor_msgs/Range` or a small custom message per logical name. `src/drivers/range.py` is unchanged.

## Out of Scope

- IMU (BNO085) — separate `src/drivers/imu.py` later
- XSHUT address programming (TCA9548A makes this unnecessary)
- Safety policy, telemetry transport, systemd units in Phase 1

## Cross-References

- [ARCHITECTURE.md](../ARCHITECTURE.md) — driver vs service boundaries
- [body-phase-2.md](../phases/body-phase-2.md) — safety sensing exit criteria
- [robot-build-gotchas.md](../phases/robot-build-gotchas.md) — I2C address conflict, bus speed
- [bom-by-phase.md](../phases/bom-by-phase.md) — sensor BOM
