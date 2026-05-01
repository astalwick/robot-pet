#!/usr/bin/env python3
"""
Print normalized Xbox 360 controller state for robot teleop.

This turns raw evdev events into the small set of values the robot will care
about: forward/back, turn, buttons, triggers, D-pad, and a deadman switch.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/diagnostics/controller-state.py
    python scripts/diagnostics/controller-state.py --device /dev/input/event4
"""

import argparse
import sys

from evdev import InputDevice, ecodes, list_devices

CONTROLLER_NAME_PARTS = ("Xbox", "X-Box", "360", "Microsoft X-Box")
SIGNED_AXIS_MAX = 32768.0
TRIGGER_MAX = 255.0

AXIS_NAMES = {
    ecodes.ABS_X: "left_x",
    ecodes.ABS_Y: "left_y",
    ecodes.ABS_RX: "right_x",
    ecodes.ABS_RY: "right_y",
    ecodes.ABS_Z: "left_trigger",
    ecodes.ABS_RZ: "right_trigger",
    ecodes.ABS_HAT0X: "dpad_x",
    ecodes.ABS_HAT0Y: "dpad_y",
}

BUTTON_NAMES = {
    304: "a",
    305: "b",
    307: "x",
    308: "y",
    310: "lb",
    311: "rb",
    314: "back",
    315: "start",
    704: "dpad_left",
    705: "dpad_right",
    706: "dpad_up",
    707: "dpad_down",
}


def list_input_devices():
    devices = []
    for path in list_devices():
        device = InputDevice(path)
        devices.append(device)
    return devices


def is_controller(device):
    return any(part in device.name for part in CONTROLLER_NAME_PARTS)


def choose_device(args, devices):
    if args.device:
        return InputDevice(args.device)

    for device in devices:
        if is_controller(device):
            return device

    return None


def apply_deadzone(value, deadzone):
    if abs(value) < deadzone:
        return 0.0
    return value


def normalize_signed_axis(value, deadzone):
    normalized = max(-1.0, min(1.0, value / SIGNED_AXIS_MAX))
    return apply_deadzone(normalized, deadzone)


def normalize_trigger(value):
    return max(0.0, min(1.0, value / TRIGGER_MAX))


def print_state(state):
    print(
        "\r"
        f"forward={state['forward']:+.2f} "
        f"turn={state['turn']:+.2f} "
        f"deadman={'held' if state['buttons']['rb'] else 'released'} "
        f"A={state['buttons']['a']} B={state['buttons']['b']} X={state['buttons']['x']} Y={state['buttons']['y']} "
        f"LT={state['left_trigger']:.2f} RT={state['right_trigger']:.2f} "
        f"dpad=({state['dpad_x']:+.0f},{state['dpad_y']:+.0f})",
        end="",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description="Print normalized Xbox controller state.")
    parser.add_argument("--device", help="Read a specific /dev/input/event* device")
    parser.add_argument("--deadzone", type=float, default=0.08, help="Stick deadzone from 0.0 to 1.0")
    args = parser.parse_args()

    devices = list_input_devices()
    device = choose_device(args, devices)
    if not device:
        print("ERROR: Could not find an Xbox controller input device.")
        print("Pair the controller with the receiver, then run scripts/diagnostics/controller-test.py.")
        sys.exit(1)

    state = {
        "left_x": 0.0,
        "left_y": 0.0,
        "right_x": 0.0,
        "right_y": 0.0,
        "left_trigger": 0.0,
        "right_trigger": 0.0,
        "dpad_x": 0.0,
        "dpad_y": 0.0,
        "forward": 0.0,
        "turn": 0.0,
        "buttons": {name: 0 for name in BUTTON_NAMES.values()},
    }

    print(f"Reading {device.path}: {device.name}")
    print("RB is the deadman switch for now. Press Ctrl+C to stop.")
    print("")
    print_state(state)

    try:
        for event in device.read_loop():
            if event.type == ecodes.EV_ABS and event.code in AXIS_NAMES:
                name = AXIS_NAMES[event.code]
                if name in ("left_trigger", "right_trigger"):
                    state[name] = normalize_trigger(event.value)
                elif name in ("dpad_x", "dpad_y"):
                    state[name] = float(event.value)
                else:
                    state[name] = normalize_signed_axis(event.value, args.deadzone)

                state["forward"] = -state["left_y"]
                state["turn"] = state["right_x"]
                print_state(state)

            if event.type == ecodes.EV_KEY and event.code in BUTTON_NAMES:
                state["buttons"][BUTTON_NAMES[event.code]] = event.value
                print_state(state)
    except PermissionError:
        print("")
        print(f"ERROR: Permission denied reading {device.path}.")
        print("Run setup.sh, log out and back in, then try again.")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
