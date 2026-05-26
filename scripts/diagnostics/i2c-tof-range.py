#!/usr/bin/env python3
"""
Print live VL53L0X range readings through the TCA9548A mux.

Stop any future robot-sensors service before running so nothing else owns the bus.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/diagnostics/i2c-tof-range.py
    python scripts/diagnostics/i2c-tof-range.py --rate 5 --duration 30
"""

import argparse
import sys
import time
from pathlib import Path

if not sys.platform.startswith("linux"):
    print("This script must run on the Pi (Linux + I2C).")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from drivers.range import DEFAULT_SENSORS, RangeDriver


def main():
    parser = argparse.ArgumentParser(
        description="Print VL53L0X distance readings (mm) via RangeDriver."
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
    args = parser.parse_args()

    interval = 1.0 / args.rate
    driver = RangeDriver(DEFAULT_SENSORS)

    print("=== I2C ToF range (TCA9548A + VL53L0X) ===")
    print(f"Channels 0–2 (cliff left / center / right), {args.rate:.1f} Hz")
    print("Ctrl-C to stop")
    print("")

    started = time.monotonic()
    try:
        while True:
            readings = driver.read_all()
            parts = []
            for reading in readings:
                if reading.ok:
                    parts.append(f"{reading.name}={reading.distance_mm}mm")
                else:
                    parts.append(f"{reading.name}=FAIL")
            print("  ".join(parts))

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
