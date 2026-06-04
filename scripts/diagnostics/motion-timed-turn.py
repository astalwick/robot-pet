#!/usr/bin/env python3
"""Send one slow, bounded timed turn through robot-motion."""

import argparse
import sys

sys.path.insert(0, "src")

from control.motion_intent import (
    DIAGNOSTIC_TURN_MAX_DURATION,
    DIAGNOSTIC_TURN_MIN_DURATION,
    request_motion_intent,
)
from telemetry.paths import DEFAULT_MOTION_INTENT_SOCKET


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate robot rotation using a bounded timed turn.")
    parser.add_argument(
        "direction",
        choices=("toward_left_wheel", "toward_right_wheel"),
        help="Direction the robot's front should rotate toward.",
    )
    parser.add_argument(
        "duration",
        type=float,
        help=f"Turn duration in seconds, from {DIAGNOSTIC_TURN_MIN_DURATION} to {DIAGNOSTIC_TURN_MAX_DURATION}.",
    )
    parser.add_argument("--socket", default=DEFAULT_MOTION_INTENT_SOCKET)
    args = parser.parse_args()

    if not DIAGNOSTIC_TURN_MIN_DURATION <= args.duration <= DIAGNOSTIC_TURN_MAX_DURATION:
        parser.error(
            f"duration must be between {DIAGNOSTIC_TURN_MIN_DURATION} and {DIAGNOSTIC_TURN_MAX_DURATION} seconds"
        )

    print(f"Turning {args.direction} for {args.duration:.2f} seconds.")
    print(
        request_motion_intent(
            args.socket,
            "diagnostic_turn",
            timeout=6.0,
            direction=args.direction,
            duration_seconds=args.duration,
        )
    )


if __name__ == "__main__":
    main()
