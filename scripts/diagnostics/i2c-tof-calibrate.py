#!/usr/bin/env python3
"""
Measure each VL53L0X sensor's raw distance and suggest a per-sensor offset_mm
so every sensor reports the same calibrated distance.

Hold the robot still over a flat floor at the real mounting geometry, then run
this. Stop the robot-sensors service first so nothing else owns the I2C bus.

The suggested offset is subtracted from the raw reading at runtime, so a sensor
that reads high gets a positive offset. Paste the printed snippet into your
sensors.json, then set the cliff trip threshold against the calibrated values.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/diagnostics/i2c-tof-calibrate.py
    python scripts/diagnostics/i2c-tof-calibrate.py --target 100 --samples 100
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

if not sys.platform.startswith("linux"):
    print("This script must run on the Pi (Linux + I2C).")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from config.sensors import load_sensors_config
from drivers.range import RangeDriver, RangeSensorConfig


def main():
    parser = argparse.ArgumentParser(
        description="Measure VL53L0X sensors and suggest per-sensor offset_mm."
    )
    parser.add_argument(
        "--target",
        type=int,
        default=None,
        help="Calibrated distance every sensor should report (default: the average of all sensors)",
    )
    parser.add_argument("--samples", type=int, default=100, help="Samples per sensor (default 100)")
    parser.add_argument("--rate", type=float, default=20.0, help="Sample rate in Hz (default 20)")
    args = parser.parse_args()

    # Measure raw distances: build the driver from the configured sensors but
    # with offsets zeroed so we calibrate against the true sensor output.
    config = load_sensors_config()
    raw_sensors = [
        RangeSensorConfig(name=sensor.name, kind=sensor.kind, channel=sensor.channel)
        for sensor in config.driver_sensors()
    ]
    try:
        driver = RangeDriver(raw_sensors)
    except RuntimeError as error:
        print(f"ERROR: {error}")
        sys.exit(1)

    print(f"Sampling {len(raw_sensors)} sensor(s), {args.samples} samples each...")
    samples = {sensor.name: [] for sensor in raw_sensors}
    interval = 1.0 / args.rate
    try:
        for _ in range(args.samples):
            for reading in driver.read_all():
                if reading.ok:
                    samples[reading.name].append(reading.distance_mm)
            time.sleep(interval)
    finally:
        driver.cleanup()

    stats = {}
    for name, values in samples.items():
        if values:
            stats[name] = (statistics.mean(values), min(values), max(values), len(values))

    if not stats:
        print("No valid readings from any sensor. Check wiring and that the floor is in range.")
        sys.exit(1)

    target = args.target if args.target is not None else round(statistics.mean(m for m, *_ in stats.values()))

    print("")
    print(f"Target (calibrated) distance: {target} mm")
    print("")
    print(f"{'sensor':<16}{'mean':>7}{'min':>7}{'max':>7}{'spread':>8}{'offset_mm':>11}")
    entries = []
    for sensor in raw_sensors:
        if sensor.name not in stats:
            print(f"{sensor.name:<16}{'  NO READINGS':>40}")
            continue
        mean, low, high, count = stats[sensor.name]
        offset = round(mean - target)
        print(f"{sensor.name:<16}{mean:>7.1f}{low:>7}{high:>7}{high - low:>8}{offset:>11}")
        entries.append((sensor.name, offset))

    print("")
    print("Paste offset_mm into the matching sensors[] entries in sensors.json:")
    print(json.dumps({name: {"offset_mm": offset} for name, offset in entries}, indent=2))


if __name__ == "__main__":
    main()
