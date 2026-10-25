#!/usr/bin/env python3
"""Turn in place and compare the IMU's rotation with wheel odometry.

For track-width calibration: the IMU says how far the robot really rotated, the
hub's cumulative wheel distances say how far each wheel rolled, and
(right - left) / rotation is the track width the wheels actually have. Scale
TRACK_WIDTH_METERS in robot_model.py to match.

Two ways to turn:

  motion-turn.py 360          IMU-closed `turn` intent. Stops on the gyro and
                              may reverse briefly to correct an overshoot.
  motion-turn.py --timed 12   Open-loop `diagnostic_turn` pulses, never
                              reversing, so gearbox backlash and reversal scrub
                              cannot leak into the result. About 17 deg/s.

Odometry is only published while the motor rail is on, so connect the gamepad
(and leave it idle) before running this.
"""

import argparse
import math
import sys
import time

sys.path.insert(0, "src")

from control.motion_intent import (
    DIAGNOSTIC_TURN_MAX_DURATION,
    DIAGNOSTIC_TURN_MIN_DURATION,
    _yaw_delta,
    request_motion_intent,
)
from robot_model import TRACK_WIDTH_METERS
from telemetry.paths import DEFAULT_MOTION_INTENT_SOCKET, DEFAULT_SUBSCRIBE_SOCKET
from telemetry.socket_client import subscribe


def read_hub(subscribe_socket: str) -> tuple[float, float, float | None]:
    """One snapshot: (left wheel m, right wheel m, IMU yaw degrees or None)."""
    snapshot = next(subscribe(subscribe_socket))
    odometry = snapshot.get("odometry")
    if not odometry:
        sys.exit("no odometry in telemetry: the motor rail is off. Connect the gamepad and leave it idle.")
    imu = (snapshot.get("sensors") or {}).get("imu") or {}
    return odometry["left_distance_m"], odometry["right_distance_m"], imu.get("yaw_degrees")


def closed_loop_turn(args) -> float:
    print(f"Turning {args.degrees:+.1f} degrees on the IMU.")
    reply = request_motion_intent(args.socket, "turn", timeout=40.0, degrees=args.degrees)
    print(reply)
    if not reply.get("ok"):
        sys.exit(1)
    # Let the final encoder read reach the hub before sampling.
    time.sleep(0.5)
    return reply["measured_degrees"]


def timed_turn(args) -> float:
    """Pulse diagnostic_turn in one direction, summing IMU yaw between pulses."""
    direction = "toward_left_wheel" if args.timed > 0 else "toward_right_wheel"
    remaining = abs(args.timed)
    _, _, last_yaw = read_hub(args.telemetry_socket)
    if last_yaw is None:
        sys.exit("no IMU yaw in telemetry.")
    turned = 0.0
    while remaining >= DIAGNOSTIC_TURN_MIN_DURATION:
        pulse = min(remaining, DIAGNOSTIC_TURN_MAX_DURATION)
        remaining -= pulse
        print(f"Turning {direction} for {pulse:.1f} s.")
        reply = request_motion_intent(
            args.socket, "diagnostic_turn", timeout=pulse + 5.0,
            direction=direction, duration_seconds=pulse,
        )
        if not reply.get("ok"):
            print(reply)
            sys.exit(1)
        # Coast to a stop and let the hub catch up before reading the yaw.
        time.sleep(1.0)
        _, _, yaw = read_hub(args.telemetry_socket)
        turned += _yaw_delta(yaw, last_yaw)
        last_yaw = yaw
    return turned


def main() -> None:
    parser = argparse.ArgumentParser(description="Turn in place and compare IMU rotation with wheel odometry.")
    parser.add_argument("degrees", type=float, nargs="?", help="Signed degrees for the IMU-closed turn, positive turns left, up to 360.")
    parser.add_argument("--timed", type=float, metavar="SECONDS", help="Open-loop turn for this many seconds instead; positive turns left.")
    parser.add_argument("--socket", default=DEFAULT_MOTION_INTENT_SOCKET)
    parser.add_argument("--telemetry-socket", default=DEFAULT_SUBSCRIBE_SOCKET)
    args = parser.parse_args()
    if (args.degrees is None) == (args.timed is None):
        parser.error("give either degrees or --timed SECONDS")

    left_before, right_before, _ = read_hub(args.telemetry_socket)
    imu_degrees = timed_turn(args) if args.timed is not None else closed_loop_turn(args)
    left_after, right_after, _ = read_hub(args.telemetry_socket)

    wheel_difference_m = (right_after - right_before) - (left_after - left_before)
    odometry_degrees = math.degrees(wheel_difference_m / TRACK_WIDTH_METERS)
    print(f"IMU rotation:        {imu_degrees:+.1f} degrees")
    print(f"odometry rotation:   {odometry_degrees:+.1f} degrees (track width {TRACK_WIDTH_METERS:.4f} m)")
    print(f"implied track width: {wheel_difference_m / math.radians(imu_degrees):.4f} m")


if __name__ == "__main__":
    main()
