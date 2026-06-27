#!/usr/bin/env python3
"""
Capture a level/forward zero orientation for the BNO085 IMU.

Stop robot-sensors first so nothing else owns the I2C bus.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/diagnostics/i2c-imu-calibrate.py
    python scripts/diagnostics/i2c-imu-calibrate.py --samples 100 --rate 20
    python scripts/diagnostics/i2c-imu-calibrate.py --mode game
"""

import argparse
import json
import sys
import time
from pathlib import Path

if not sys.platform.startswith("linux"):
    print("This script must run on the Pi (Linux + I2C).")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from drivers.imu import (  # noqa: E402
    average_quaternions,
    quaternion_to_euler_degrees,
    read_bno085_quaternion,
    relative_quaternion,
)


def open_bno085(channel, address, mux_address, mode):
    try:
        import board
        import adafruit_tca9548a
        from adafruit_bno08x import (
            BNO_REPORT_GAME_ROTATION_VECTOR,
            BNO_REPORT_ROTATION_VECTOR,
        )
        from adafruit_bno08x.i2c import BNO08X_I2C
    except ImportError as error:
        raise RuntimeError(
            "adafruit-circuitpython-bno08x not installed "
            "(run: pip install -e . from the repo venv on the Pi)"
        ) from error

    i2c = board.I2C()
    mux = adafruit_tca9548a.TCA9548A(i2c, address=mux_address)
    sensor = BNO08X_I2C(mux[channel], address=address)
    if mode == "game":
        sensor.enable_feature(BNO_REPORT_GAME_ROTATION_VECTOR)
    else:
        sensor.enable_feature(BNO_REPORT_ROTATION_VECTOR)
    return sensor


def collect_zero_quaternion(sensor, mode, samples, rate):
    interval = 1.0 / rate
    quaternions = []
    for _ in range(samples):
        quaternions.append(read_bno085_quaternion(sensor, mode))
        time.sleep(interval)
    return average_quaternions(quaternions)


def direction(value, positive_label, negative_label, neutral_label):
    if abs(value) < 1.0:
        return neutral_label
    if value > 0:
        return positive_label
    return negative_label


def main():
    parser = argparse.ArgumentParser(
        description="Capture BNO085 zero orientation and stream calibrated pitch/roll/yaw."
    )
    parser.add_argument(
        "--mux-address",
        type=lambda value: int(value, 0),
        default=0x70,
        help="TCA9548A address (default 0x70)",
    )
    parser.add_argument(
        "--channel",
        type=int,
        default=5,
        help="Mux channel for the BNO085 (default 5 = Grove port 6)",
    )
    parser.add_argument(
        "--address",
        type=lambda value: int(value, 0),
        default=0x4A,
        help="BNO085 I2C address (default 0x4a)",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=50,
        help="Zero samples to average (default 50)",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=20.0,
        help="Sample rate in Hz (default 20)",
    )
    parser.add_argument(
        "--mode",
        choices=("game", "rotation"),
        default="game",
        help="Use relative game vector or north-referenced rotation vector (default game)",
    )
    args = parser.parse_args()

    print("=== BNO085 orientation calibration ===")
    print(
        f"Mux 0x{args.mux_address:02x}, channel {args.channel} "
        f"(Grove port {args.channel + 1}), IMU 0x{args.address:02x}, mode {args.mode}"
    )
    print("")
    print("Place the robot level and facing its normal forward direction.")
    input("Press Enter when it is still...")

    try:
        sensor = open_bno085(args.channel, args.address, args.mux_address, args.mode)
        print(f"Sampling {args.samples} orientation readings...")
        zero_quaternion = collect_zero_quaternion(sensor, args.mode, args.samples, args.rate)
    except RuntimeError as error:
        print(f"ERROR: {error}")
        sys.exit(1)

    print("")
    print("Zero orientation:")
    print(
        json.dumps(
            {
                "imu": {
                    "enabled": True,
                    "kind": "bno085",
                    "mux_channel": args.channel,
                    "address": f"0x{args.address:02x}",
                    "mode": args.mode,
                    "zero_quaternion": [round(value, 8) for value in zero_quaternion],
                    "axis_map": {
                        "yaw_degrees": "-pitch_degrees",
                        "pitch_degrees": "+roll_degrees",
                        "roll_degrees": "+yaw_degrees",
                    },
                }
            },
            indent=2,
        )
    )
    print("")
    print("Live calibrated orientation. Press Ctrl-C to stop.")
    print("Signs: positive turn=left, nose=front up, side=left up.")
    print("")

    try:
        while True:
            current_quaternion = read_bno085_quaternion(sensor, args.mode)
            sensor_roll, sensor_pitch, sensor_yaw = quaternion_to_euler_degrees(
                relative_quaternion(zero_quaternion, current_quaternion)
            )
            turn = -sensor_pitch
            nose = sensor_roll
            side = sensor_yaw
            print(
                f"turn={abs(turn):6.2f} deg "
                f"{direction(turn, 'left ', 'right', 'still')}  "
                f"nose={abs(nose):6.2f} deg "
                f"{direction(nose, 'up  ', 'down', 'level')}  "
                f"side={abs(side):6.2f} deg "
                f"{direction(side, 'left up ', 'right up', 'level   ')}",
                end="\r",
                flush=True,
            )
            time.sleep(1.0 / args.rate)
    except KeyboardInterrupt:
        print("")


if __name__ == "__main__":
    main()
