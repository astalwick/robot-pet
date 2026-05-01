#!/usr/bin/env python3
"""
Drive the RoboClaw at low duty from an Xbox 360 controller.

This is a constrained diagnostic, not full teleop. Keep the wheels off the
ground. RB is the deadman switch; releasing it sends stop.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/diagnostics/controller-roboclaw-test.py
    python scripts/diagnostics/controller-roboclaw-test.py --max-duty 0.05
"""

import argparse
import select
import sys

from basicmicro import Basicmicro
from evdev import InputDevice, ecodes, list_devices

CONTROLLER_NAME_PARTS = ("Xbox", "X-Box", "360", "Microsoft X-Box")
SIGNED_AXIS_MAX = 32768.0
MAX_DUTY = 32767

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


def set_duty(rc, address, m1, m2):
    ok_m1 = rc.DutyM1(address, m1)
    ok_m2 = rc.DutyM2(address, m2)
    return ok_m1 and ok_m2


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


def print_status(forward, turn, deadman, m1, m2):
    print(
        "\r"
        f"forward={forward:+.2f} "
        f"turn={turn:+.2f} "
        f"deadman={'held' if deadman else 'released'} "
        f"DutyM1={m1:+6d} DutyM2={m2:+6d}",
        end="",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description="Low-duty RoboClaw test from an Xbox controller.")
    parser.add_argument("--device", help="Read a specific /dev/input/event* controller device")
    parser.add_argument("--port", default="/dev/serial0", help="RoboClaw serial port")
    parser.add_argument("--address", type=parse_address, default=0x80, help="RoboClaw packet serial address")
    parser.add_argument("--baud", type=int, default=38400, help="RoboClaw serial baud rate")
    parser.add_argument("--max-duty", type=float, default=0.08, help="Maximum duty fraction from 0.0 to 1.0")
    parser.add_argument("--deadzone", type=float, default=0.08, help="Stick deadzone from 0.0 to 1.0")
    args = parser.parse_args()

    controller = choose_device(args.device)
    if not controller:
        print("ERROR: Could not find an Xbox controller input device.")
        print("Pair the controller with the receiver, then run scripts/diagnostics/controller-test.py.")
        sys.exit(1)

    duty_limit = int(MAX_DUTY * args.max_duty)
    forward = 0.0
    turn = 0.0
    deadman = False
    last_m1 = 0
    last_m2 = 0
    last_display = None
    roboclaw_ready = False

    print("=== Controller RoboClaw Test ===")
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

        if not set_duty(rc, args.address, 0, 0):
            print("ERROR: Initial stop command was not acknowledged.")
            sys.exit(1)

        print(f"Max duty: {args.max_duty:.0%} ({duty_limit} / {MAX_DUTY})")
        print("")
        print_status(forward, turn, deadman, last_m1, last_m2)

        while True:
            ready, _, _ = select.select([controller.fd], [], [], 0.05)

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
                m1 = int(left * duty_limit)
                m2 = int(right * duty_limit)
            else:
                m1 = 0
                m2 = 0

            if (m1, m2) != (last_m1, last_m2):
                if not set_duty(rc, args.address, m1, m2):
                    print("\nERROR: RoboClaw duty command was not acknowledged.")
                    sys.exit(1)
                last_m1 = m1
                last_m2 = m2

            display = (forward, turn, deadman, last_m1, last_m2)
            if display != last_display:
                print_status(forward, turn, deadman, last_m1, last_m2)
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
            set_duty(rc, args.address, 0, 0)
        rc.close()
        print("Motors stopped." if roboclaw_ready else "Exited without enabling motor commands.")


if __name__ == "__main__":
    main()
