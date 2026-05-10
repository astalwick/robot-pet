#!/usr/bin/env python3
"""
Read RoboClaw configuration values without commanding the motors.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    sudo systemctl stop gamepad-teleop
    python scripts/diagnostics/roboclaw-read-config.py
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


def print_voltage_limits(label, result):
    if result is None:
        print(f"{label}: unavailable in this basicmicro library")
    elif result[0]:
        print(f"{label}: min={result[1] / 10.0:.1f}V max={result[2] / 10.0:.1f}V")
    else:
        print(f"{label}: {result[1]}")


def print_velocity_pid(label, result):
    if result is None:
        print(f"{label}: unavailable in this basicmicro library")
    elif result[0]:
        print(f"{label}: P={result[1]:.6g} I={result[2]:.6g} D={result[3]:.6g} QPPS={result[4]}")
    else:
        print(f"{label}: {result[1]}")


def print_max_current(label, result):
    if result is None:
        print(f"{label}: unavailable in this basicmicro library")
    elif result[0]:
        print(f"{label}: max={result[1] / 100.0:.2f}A raw={result[1]} min={result[2] / 100.0:.2f}A")
    else:
        print(f"{label}: {result[1]}")


def print_simple(label, result, formatter=str):
    if result is None:
        print(f"{label}: unavailable in this basicmicro library")
    elif result[0]:
        print(f"{label}: {formatter(result)}")
    else:
        print(f"{label}: {result[1]}")


def main():
    parser = argparse.ArgumentParser(description="Read RoboClaw config without commanding motors.")
    parser.add_argument("--port", default="/dev/serial0", help="Serial port to use")
    parser.add_argument("--address", type=parse_address, default=0x80, help="Packet serial address, e.g. 0x80 or 128")
    parser.add_argument("--baud", type=int, default=38400, help="Serial baud rate")
    args = parser.parse_args()

    print("=== RoboClaw Read Config ===")
    print("")
    print("This script only reads values. It does not send motor commands.")
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
        print("")

        main_battery = read_or_none(rc, "ReadMainBatteryVoltage", args.address)
        logic_battery = read_or_none(rc, "ReadLogicBatteryVoltage", args.address)
        print_simple("Main battery now", main_battery, lambda value: f"{value[1] / 10.0:.1f}V")
        print_simple("Logic battery now", logic_battery, lambda value: f"{value[1] / 10.0:.1f}V")
        print_voltage_limits("Main battery limits", read_or_none(rc, "ReadMinMaxMainVoltages", args.address))
        print_voltage_limits("Logic battery limits", read_or_none(rc, "ReadMinMaxLogicVoltages", args.address))

        print("")
        print_velocity_pid("M1 velocity PID", read_or_none(rc, "ReadM1VelocityPID", args.address))
        print_velocity_pid("M2 velocity PID", read_or_none(rc, "ReadM2VelocityPID", args.address))

        print("")
        currents = read_or_none(rc, "ReadCurrents", args.address)
        print_simple(
            "Current draw now",
            currents,
            lambda value: f"M1={value[1] / 100.0:.2f}A M2={value[2] / 100.0:.2f}A",
        )
        print_max_current("M1 current limit", read_or_none(rc, "ReadM1MaxCurrent", args.address))
        print_max_current("M2 current limit", read_or_none(rc, "ReadM2MaxCurrent", args.address))

        print("")
        print_simple(
            "Encoder modes",
            read_or_none(rc, "ReadEncoderModes", args.address),
            lambda value: f"M1={value[1]} M2={value[2]}",
        )
        print_simple("Config word", read_or_none(rc, "GetConfig", args.address), lambda value: f"0x{value[1]:04X}")
        print_simple("Status word", read_or_none(rc, "ReadError", args.address), lambda value: f"0x{value[1]:08X}")

        print("")
        print("Expected quick checks for this robot:")
        print("  - Main battery min should be around 9.6V for a conservative 3S LiPo cutoff.")
        print("  - Current limits around 5.00A are what the old checklist intended.")
        print("  - M1/M2 QPPS should match the values Motion Studio auto-tuned.")
    finally:
        rc.close()


if __name__ == "__main__":
    main()
