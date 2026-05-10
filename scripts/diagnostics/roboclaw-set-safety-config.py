#!/usr/bin/env python3
"""
Set RoboClaw battery cutoff and motor current limits.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    sudo systemctl stop gamepad-teleop
    python scripts/diagnostics/roboclaw-set-safety-config.py
    python scripts/diagnostics/roboclaw-set-safety-config.py --yes
"""

import argparse
import sys

from basicmicro import Basicmicro


def format_version(version):
    if isinstance(version, bytes):
        return version.decode("ascii", errors="replace").strip()
    return str(version).strip()


def parse_address(value):
    return int(value, 0)


def read_or_none(rc, method_name, address):
    method = getattr(rc, method_name, None)
    if method is None:
        return None

    try:
        result = method(address)
    except Exception as exc:
        return False, str(exc)

    if result and result[0]:
        return result
    return False, "read failed"


def set_main_battery_min(rc, address, min_tenths, max_tenths):
    if hasattr(rc, "SetMainVoltages"):
        try:
            return rc.SetMainVoltages(address, min_tenths, max_tenths, 0)
        except TypeError:
            return rc.SetMainVoltages(address, min_tenths, max_tenths)
    return rc.SetMinVoltageMainBattery(address, min_tenths)


def set_motor_current_limit(method, address, current_raw):
    try:
        return method(address, current_raw, 0)
    except TypeError:
        return method(address, current_raw)


def print_voltage_limits(label, result):
    if result is None:
        print(f"{label}: unavailable in this basicmicro library")
    elif result[0]:
        print(f"{label}: min={result[1] / 10.0:.1f}V max={result[2] / 10.0:.1f}V")
    else:
        print(f"{label}: {result[1]}")


def print_max_current(label, result):
    if result is None:
        print(f"{label}: unavailable in this basicmicro library")
    elif result[0]:
        print(f"{label}: max={result[1] / 100.0:.2f}A raw={result[1]} min={result[2] / 100.0:.2f}A")
    else:
        print(f"{label}: {result[1]}")


def main():
    parser = argparse.ArgumentParser(description="Set RoboClaw battery cutoff and current limits.")
    parser.add_argument("--port", default="/dev/serial0", help="Serial port to use")
    parser.add_argument("--address", type=parse_address, default=0x80, help="Packet serial address, e.g. 0x80 or 128")
    parser.add_argument("--baud", type=int, default=38400, help="Serial baud rate")
    parser.add_argument("--main-min-volts", type=float, default=9.6, help="Main battery cutoff voltage")
    parser.add_argument("--main-max-volts", type=float, default=None, help="Main battery max voltage; defaults to current setting")
    parser.add_argument("--motor-current-amps", type=float, default=10.0, help="M1/M2 current limit")
    parser.add_argument("--yes", action="store_true", help="Apply without interactive confirmation")
    args = parser.parse_args()

    print("=== RoboClaw Set Safety Config ===")
    print("")
    print("This script changes RoboClaw config and writes it to NVM.")
    print("It does not send motor commands.")
    print(f"Connecting to RoboClaw at {args.port} ({args.baud} baud, address 0x{args.address:02X})...")

    try:
        rc = Basicmicro(args.port, args.baud)
        rc.Open()
    except Exception as exc:
        print(f"ERROR: Could not connect: {exc}")
        print("\nTroubleshooting:")
        print("  - Is the RoboClaw powered on?")
        print(f"  - Is {args.port} available? (ls -l {args.port})")
        print("  - Is gamepad-teleop stopped so it is not holding the serial port?")
        sys.exit(1)

    try:
        version = read_or_none(rc, "ReadVersion", args.address)
        if not version or not version[0]:
            print("ERROR: RoboClaw did not respond to ReadVersion.")
            print("Check packet serial mode, baud rate, address, wiring, and power.")
            sys.exit(1)
        print(f"RoboClaw version: {format_version(version[1])}")

        main_limits = read_or_none(rc, "ReadMinMaxMainVoltages", args.address)
        max_tenths = int(round(args.main_max_volts * 10)) if args.main_max_volts is not None else 137
        if main_limits and main_limits[0] and args.main_max_volts is None:
            max_tenths = main_limits[2]

        min_tenths = int(round(args.main_min_volts * 10))
        current_raw = int(round(args.motor_current_amps * 100))

        print("")
        print("Current values:")
        print_voltage_limits("Main battery limits", main_limits)
        print_max_current("M1 current limit", read_or_none(rc, "ReadM1MaxCurrent", args.address))
        print_max_current("M2 current limit", read_or_none(rc, "ReadM2MaxCurrent", args.address))

        print("")
        print("Will set:")
        print(f"  Main battery limits: min={min_tenths / 10.0:.1f}V max={max_tenths / 10.0:.1f}V")
        print(f"  M1 current limit: {current_raw / 100.0:.2f}A")
        print(f"  M2 current limit: {current_raw / 100.0:.2f}A")

        if not args.yes:
            answer = input("\nType 'yes' to apply and write NVM: ").strip().lower()
            if answer != "yes":
                print("Aborted.")
                return

        print("")
        if not set_main_battery_min(rc, args.address, min_tenths, max_tenths):
            print("ERROR: Failed to set main battery limits.")
            sys.exit(1)
        print("Set main battery limits.")

        if not set_motor_current_limit(rc.SetM1MaxCurrent, args.address, current_raw):
            print("ERROR: Failed to set M1 current limit.")
            sys.exit(1)
        print("Set M1 current limit.")

        if not set_motor_current_limit(rc.SetM2MaxCurrent, args.address, current_raw):
            print("ERROR: Failed to set M2 current limit.")
            sys.exit(1)
        print("Set M2 current limit.")

        if not rc.WriteNVM(args.address):
            print("ERROR: Failed to write settings to NVM.")
            sys.exit(1)
        print("Wrote settings to NVM.")

        print("")
        print("Readback:")
        print_voltage_limits("Main battery limits", read_or_none(rc, "ReadMinMaxMainVoltages", args.address))
        print_max_current("M1 current limit", read_or_none(rc, "ReadM1MaxCurrent", args.address))
        print_max_current("M2 current limit", read_or_none(rc, "ReadM2MaxCurrent", args.address))
    finally:
        rc.close()


if __name__ == "__main__":
    main()
