#!/usr/bin/env python3
"""
Drive the RoboClaw in closed-loop velocity mode from an Xbox 360 controller.

This uses RoboClaw's encoder/PID speed control instead of raw duty. Keep the
wheels off the ground for first runs. RB is the deadman switch; releasing it
sends zero speed.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/diagnostics/controller-roboclaw-speed-test.py
    python scripts/diagnostics/controller-roboclaw-speed-test.py --speed-scale 0.15
"""

import argparse
import select
import sys
import time

from basicmicro import Basicmicro
from evdev import InputDevice, ecodes, list_devices

CONTROLLER_NAME_PARTS = ("Xbox", "X-Box", "360", "Microsoft X-Box")
SIGNED_AXIS_MAX = 32768.0

LEFT_Y = ecodes.ABS_Y
RIGHT_X = ecodes.ABS_RX
RB = 311


def format_version(version):
    if isinstance(version, bytes):
        return version.decode("ascii", errors="replace").strip()
    return str(version).strip()


def parse_address(value):
    return int(value, 0)


def list_input_devices():
    devices = []
    for path in list_devices():
        device = InputDevice(path)
        devices.append(device)
    return devices


def is_controller(device):
    return any(part in device.name for part in CONTROLLER_NAME_PARTS)


def choose_device(device_path):
    if device_path:
        return InputDevice(device_path)

    for device in list_input_devices():
        if is_controller(device):
            return device

    return None


def normalize_axis(value, deadzone):
    normalized = max(-1.0, min(1.0, value / SIGNED_AXIS_MAX))
    if abs(normalized) < deadzone:
        return 0.0
    return normalized


def mix_arcade_drive(forward, turn):
    left = max(-1.0, min(1.0, forward + turn))
    right = max(-1.0, min(1.0, forward - turn))
    return left, right


def read_version(rc, address):
    result = rc.ReadVersion(address)
    if result[0]:
        return format_version(result[1])
    return None


def read_voltage(rc, address):
    result = rc.ReadMainBatteryVoltage(address)
    if result[0]:
        return result[1] / 10.0
    return None


def set_speed(rc, address, m1, m2):
    return rc.SpeedM1M2(address, m1, m2)


def read_motor_speed(rc, address):
    m1 = rc.ReadSpeedM1(address)
    m2 = rc.ReadSpeedM2(address)
    actual_m1 = m1[1] if m1[0] else None
    actual_m2 = m2[1] if m2[0] else None
    return actual_m1, actual_m2


def format_actual(value):
    if value is None:
        return "   n/a"
    return f"{value:+6d}"


def print_status(forward, turn, deadman, target_m1, target_m2, actual_m1, actual_m2):
    print(
        "\r"
        f"forward={forward:+.2f} "
        f"turn={turn:+.2f} "
        f"deadman={'held' if deadman else 'released'} "
        f"target=({target_m1:+5d},{target_m2:+5d}) "
        f"actual=({format_actual(actual_m1)},{format_actual(actual_m2)})",
        end="",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description="Closed-loop RoboClaw speed test from an Xbox controller.")
    parser.add_argument("--device", help="Read a specific /dev/input/event* controller device")
    parser.add_argument("--port", default="/dev/serial0", help="RoboClaw serial port")
    parser.add_argument("--address", type=parse_address, default=0x80, help="RoboClaw packet serial address")
    parser.add_argument("--baud", type=int, default=38400, help="RoboClaw serial baud rate")
    parser.add_argument("--qpps", type=int, default=2425, help="Configured RoboClaw max speed in encoder counts/sec")
    parser.add_argument("--speed-scale", type=float, default=0.25, help="Fraction of --qpps allowed by this test")
    parser.add_argument("--deadzone", type=float, default=0.15, help="Stick deadzone from 0.0 to 1.0")
    parser.add_argument("--status-interval", type=float, default=0.2, help="Seconds between actual speed refreshes")
    args = parser.parse_args()

    controller = choose_device(args.device)
    if not controller:
        print("ERROR: Could not find an Xbox controller input device.")
        print("Pair the controller with the receiver, then run scripts/diagnostics/controller-test.py.")
        sys.exit(1)

    speed_limit = int(args.qpps * args.speed_scale)
    forward = 0.0
    turn = 0.0
    deadman = False
    target_m1 = 0
    target_m2 = 0
    actual_m1 = None
    actual_m2 = None
    last_target = None
    last_display = None
    last_status_at = 0.0
    roboclaw_ready = False

    print("=== Controller RoboClaw Speed Test ===")
    print("")
    print("WHEELS OFF THE GROUND.")
    print("RB is the deadman switch. Release RB or Ctrl+C to stop.")
    print("")
    print(f"Reading controller: {controller.path} ({controller.name})")
    print(f"Connecting to RoboClaw at {args.port} ({args.baud} baud, address 0x{args.address:02X})...")

    try:
        rc = Basicmicro(args.port, args.baud)
        rc.Open()
    except Exception as e:
        print(f"ERROR: Could not connect to RoboClaw: {e}")
        sys.exit(1)

    try:
        version = read_version(rc, args.address)
        if not version:
            print("ERROR: RoboClaw did not respond to ReadVersion.")
            print("Check that it is powered on and wired to the Pi UART.")
            sys.exit(1)
        print(f"RoboClaw version: {version}")
        roboclaw_ready = True

        voltage = read_voltage(rc, args.address)
        if voltage:
            print(f"Main battery: {voltage:.1f}V")

        if not set_speed(rc, args.address, 0, 0):
            print("ERROR: Initial zero-speed command was not acknowledged.")
            sys.exit(1)

        print(f"Configured QPPS cap: {args.qpps}")
        print(f"Test speed scale: {args.speed_scale:.0%} ({speed_limit} counts/sec max target)")
        print("")
        print_status(forward, turn, deadman, target_m1, target_m2, actual_m1, actual_m2)

        while True:
            ready, _, _ = select.select([controller.fd], [], [], 0.05)
            now = time.monotonic()

            if ready:
                for event in controller.read():
                    if event.type == ecodes.EV_ABS and event.code == LEFT_Y:
                        forward = -normalize_axis(event.value, args.deadzone)
                    elif event.type == ecodes.EV_ABS and event.code == RIGHT_X:
                        turn = normalize_axis(event.value, args.deadzone)
                    elif event.type == ecodes.EV_KEY and event.code == RB:
                        deadman = event.value == 1

            left, right = mix_arcade_drive(forward, turn)

            if deadman:
                target_m1 = int(left * speed_limit)
                target_m2 = int(right * speed_limit)
            else:
                target_m1 = 0
                target_m2 = 0

            target = (target_m1, target_m2)
            if target != last_target:
                if not set_speed(rc, args.address, target_m1, target_m2):
                    print("\nERROR: RoboClaw speed command was not acknowledged.")
                    sys.exit(1)
                last_target = target

            if now - last_status_at >= args.status_interval:
                actual_m1, actual_m2 = read_motor_speed(rc, args.address)
                last_status_at = now

            display = (forward, turn, deadman, target_m1, target_m2, actual_m1, actual_m2)
            if display != last_display:
                print_status(forward, turn, deadman, target_m1, target_m2, actual_m1, actual_m2)
                last_display = display
    except PermissionError:
        print("")
        print(f"ERROR: Permission denied reading {controller.path}.")
        print("Run setup.sh, log out and back in, then try again.")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        if roboclaw_ready:
            set_speed(rc, args.address, 0, 0)
        rc.close()
        print("Motors stopped." if roboclaw_ready else "Exited without enabling motor commands.")


if __name__ == "__main__":
    main()
