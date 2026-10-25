#!/usr/bin/env python3
"""Turn in place by an IMU-measured angle and compare it with wheel odometry.

For track-width calibration: the turn stops on the IMU, so the reply's
measured_degrees is the real rotation. The hub's cumulative wheel distances say
how far each wheel rolled, and (right - left) / rotation is the track width the
wheels actually have. Scale TRACK_WIDTH_METERS in robot_model.py to match.

Odometry is only published while the motor rail is on, so connect the gamepad
(and leave it idle) before running this.
"""

import argparse
import math
import sys
import time

sys.path.insert(0, "src")

from control.motion_intent import request_motion_intent
from robot_model import TRACK_WIDTH_METERS
from telemetry.paths import DEFAULT_MOTION_INTENT_SOCKET, DEFAULT_SUBSCRIBE_SOCKET
from telemetry.socket_client import subscribe


def wheel_distances(subscribe_socket: str) -> tuple[float, float]:
    snapshot = next(subscribe(subscribe_socket))
    odometry = snapshot.get("odometry")
    if not odometry:
        sys.exit("no odometry in telemetry: the motor rail is off. Connect the gamepad and leave it idle.")
    return odometry["left_distance_m"], odometry["right_distance_m"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Turn in place and compare IMU rotation with wheel odometry.")
    parser.add_argument("degrees", type=float, help="Signed degrees, positive turns left, up to 360.")
    parser.add_argument("--socket", default=DEFAULT_MOTION_INTENT_SOCKET)
    parser.add_argument("--telemetry-socket", default=DEFAULT_SUBSCRIBE_SOCKET)
    args = parser.parse_args()

    left_before, right_before = wheel_distances(args.telemetry_socket)
    print(f"Turning {args.degrees:+.1f} degrees.")
    reply = request_motion_intent(args.socket, "turn", timeout=40.0, degrees=args.degrees)
    print(reply)
    if not reply.get("ok"):
        return
    # Let the final encoder read reach the hub before sampling.
    time.sleep(0.5)
    left_after, right_after = wheel_distances(args.telemetry_socket)

    wheel_difference_m = (right_after - right_before) - (left_after - left_before)
    odometry_degrees = math.degrees(wheel_difference_m / TRACK_WIDTH_METERS)
    imu_degrees = reply["measured_degrees"]
    print(f"IMU rotation:       {imu_degrees:+.1f} degrees")
    print(f"odometry rotation:  {odometry_degrees:+.1f} degrees (track width {TRACK_WIDTH_METERS:.4f} m)")
    print(f"implied track width: {wheel_difference_m / math.radians(imu_degrees):.4f} m")


if __name__ == "__main__":
    main()
