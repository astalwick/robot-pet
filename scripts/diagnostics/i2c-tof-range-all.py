#!/usr/bin/env python3
"""
Probe every TCA9548A mux channel for VL53L0X or VL53L1X and print range (mm).

Stop robot-sensors first so nothing else owns the I2C bus.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/diagnostics/i2c-tof-range-all.py
    python scripts/diagnostics/i2c-tof-range-all.py --once
    python scripts/diagnostics/i2c-tof-range-all.py --rate 5 --duration 30
"""

import argparse
import sys
import time
from pathlib import Path

if not sys.platform.startswith("linux"):
    print("This script must run on the Pi (Linux + I2C).")
    sys.exit(1)

from smbus2 import SMBus

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from drivers.range import RangeDriver, RangeSensorConfig

MUX_DEFAULT = 0x70
TOF_DEFAULT = 0x29
NUM_MUX_CHANNELS = 8

VL53L0X_MODEL_ID = 0xEE
VL53L0X_MODEL_ID_REG = 0xC0

VL53L1X_WHO_AM_I = 0xEACC
VL53L1X_WHO_AM_I_REG = 0x010F


def select_mux_channel(bus, mux_address, channel):
    bus.write_byte(mux_address, 1 << channel)


def read_reg16(bus, tof_address, register):
    bus.write_i2c_block_data(tof_address, register >> 8, [register & 0xFF])
    data = bus.read_i2c_block_data(tof_address, 0, 2)
    return (data[0] << 8) | data[1]


def probe_channel(bus, tof_address):
    try:
        model_id = bus.read_byte_data(tof_address, VL53L0X_MODEL_ID_REG)
    except OSError:
        return None
    if model_id == VL53L0X_MODEL_ID:
        return "vl53l0x"
    try:
        who_am_i = read_reg16(bus, tof_address, VL53L1X_WHO_AM_I_REG)
    except OSError:
        return None
    if who_am_i == VL53L1X_WHO_AM_I:
        return "vl53l1x"
    return None


def discover_sensors(bus, mux_address, tof_address):
    try:
        bus.read_byte(mux_address)
    except OSError as error:
        raise RuntimeError(
            f"TCA9548A not responding at 0x{mux_address:02x} "
            f"(is I2C enabled and wiring correct?): {error}"
        ) from error

    found = []
    for channel in range(NUM_MUX_CHANNELS):
        select_mux_channel(bus, mux_address, channel)
        kind = probe_channel(bus, tof_address)
        if kind is not None:
            found.append(RangeSensorConfig(f"ch{channel}", kind, channel))
    return found


def format_readings(readings):
    parts = []
    for reading in readings:
        port = reading.channel + 1
        label = f"ch{reading.channel}/port{port} {reading.kind}"
        if reading.ok:
            parts.append(f"{label}={reading.distance_mm}mm")
        else:
            parts.append(f"{label}=FAIL")
    return "  ".join(parts)


def main():
    parser = argparse.ArgumentParser(
        description="Probe all mux channels for VL53 sensors and print range (mm)."
    )
    parser.add_argument("--bus", type=int, default=1, help="I2C bus number (default 1)")
    parser.add_argument(
        "--mux-address",
        type=lambda value: int(value, 0),
        default=MUX_DEFAULT,
        help="TCA9548A address (default 0x70)",
    )
    parser.add_argument(
        "--tof-address",
        type=lambda value: int(value, 0),
        default=TOF_DEFAULT,
        help="VL53 sensor address (default 0x29)",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=5.0,
        help="Sample rate in Hz (default 5)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Stop after this many seconds (default: run until Ctrl-C)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print one sample per sensor and exit",
    )
    args = parser.parse_args()

    with SMBus(args.bus) as bus:
        try:
            sensors = discover_sensors(bus, args.mux_address, args.tof_address)
        except RuntimeError as error:
            print(f"ERROR: {error}")
            sys.exit(1)

    if not sensors:
        print("No VL53L0X or VL53L1X sensors found on mux channels 0–7.")
        sys.exit(1)

    try:
        driver = RangeDriver(sensors, mux_address=args.mux_address, range_address=args.tof_address)
    except RuntimeError as error:
        print(f"ERROR: {error}")
        sys.exit(1)

    print("=== I2C ToF range — all mux channels ===")
    for config in sensors:
        print(f"  ch{config.channel} (Grove port {config.channel + 1}): {config.kind}")
    if args.once:
        print("")
        print(format_readings(driver.read_all()))
        driver.cleanup()
        return

    print(f"{args.rate:.1f} Hz — Ctrl-C to stop")
    print("")

    interval = 1.0 / args.rate
    started = time.monotonic()
    try:
        while True:
            print(format_readings(driver.read_all()))
            if args.duration is not None and time.monotonic() - started >= args.duration:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("")
        print("Stopped.")
    finally:
        driver.cleanup()


if __name__ == "__main__":
    main()
