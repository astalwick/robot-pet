#!/usr/bin/env python3
"""Send one encoder-distance move through robot-motion.

For wheel calibration: command a known distance, measure what the robot actually
drove, and scale WHEEL_DIAMETER_METERS in robot_model.py by actual / commanded.
"""

import argparse
import sys

sys.path.insert(0, "src")

from control.motion_intent import MOVE_MAX_DISTANCE_METERS, request_motion_intent
from telemetry.paths import DEFAULT_MOTION_INTENT_SOCKET


def main() -> None:
    parser = argparse.ArgumentParser(description="Drive straight by a signed encoder distance.")
    parser.add_argument(
        "distance",
        type=float,
        help=f"Meters to drive, up to {MOVE_MAX_DISTANCE_METERS}; negative drives backward.",
    )
    parser.add_argument("--socket", default=DEFAULT_MOTION_INTENT_SOCKET)
    args = parser.parse_args()

    print(f"Moving {args.distance:+.2f} m.")
    print(request_motion_intent(args.socket, "move", timeout=40.0, distance_meters=args.distance))


if __name__ == "__main__":
    main()
