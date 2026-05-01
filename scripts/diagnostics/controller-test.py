#!/usr/bin/env python3
"""
Print gamepad input events from the Xbox 360 receiver/controller.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/diagnostics/controller-test.py
    python scripts/diagnostics/controller-test.py --list
    python scripts/diagnostics/controller-test.py --device /dev/input/event4
"""

import argparse
import sys

from evdev import InputDevice, ecodes, list_devices

CONTROLLER_NAME_PARTS = ("Xbox", "X-Box", "360", "Microsoft X-Box")


def list_input_devices():
    devices = []
    for path in list_devices():
        device = InputDevice(path)
        devices.append(device)
    return devices


def print_devices(devices):
    print("Input devices:")
    for device in devices:
        print(f"  {device.path}: {device.name}")


def is_controller(device):
    return any(part in device.name for part in CONTROLLER_NAME_PARTS)


def choose_device(args, devices):
    if args.device:
        return InputDevice(args.device)

    for device in devices:
        if is_controller(device):
            return device

    return None


def event_name(event):
    if event.type == ecodes.EV_KEY:
        return ecodes.KEY.get(event.code, event.code)
    if event.type == ecodes.EV_ABS:
        return ecodes.ABS.get(event.code, event.code)
    return event.code


def main():
    parser = argparse.ArgumentParser(description="Print Xbox controller button and axis events.")
    parser.add_argument("--list", action="store_true", help="List input devices and exit")
    parser.add_argument("--device", help="Read a specific /dev/input/event* device")
    args = parser.parse_args()

    devices = list_input_devices()
    print_devices(devices)

    if args.list:
        return

    device = choose_device(args, devices)
    if not device:
        print("")
        print("ERROR: Could not find an Xbox controller input device.")
        print("Pair the controller with the receiver, then run this again.")
        sys.exit(1)

    print("")
    print(f"Reading {device.path}: {device.name}")
    print("Move sticks and press buttons. Press Ctrl+C to stop.")
    print("")

    try:
        for event in device.read_loop():
            if event.type in (ecodes.EV_KEY, ecodes.EV_ABS):
                print(f"{event.sec}.{event.usec:06d} {ecodes.EV[event.type]} {event_name(event)} {event.value}")
    except PermissionError:
        print("")
        print(f"ERROR: Permission denied reading {device.path}.")
        print("Run setup.sh, log out and back in, then try again.")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
