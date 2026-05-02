#!/usr/bin/env python3
"""
Read RoboClaw motor current telemetry without sending motor commands.

Use this with gamepad-teleop stopped to check whether idle current readings come
from the RoboClaw itself or from the teleop control loop.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    sudo systemctl stop gamepad-teleop
    python scripts/diagnostics/roboclaw-read-currents.py
    python scripts/diagnostics/roboclaw-read-currents.py --samples 20 --interval 0.5
"""

import argparse
import statistics
import sys
import time

from basicmicro import Basicmicro


def format_version(version):
    if isinstance(version, bytes):
        return version.decode("ascii", errors="replace").strip()
    return str(version).strip()


def parse_address(value):
    return int(value, 0)


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


def read_currents(rc, address):
    result = rc.ReadCurrents(address)
    if result[0]:
        return result[1], result[2], result[1] / 100.0, result[2] / 100.0
    return None


def print_summary(label, values):
    print(
        f"{label}: avg={statistics.mean(values):+.2f}A "
        f"min={min(values):+.2f}A max={max(values):+.2f}A"
    )


def main():
    parser = argparse.ArgumentParser(description="Read RoboClaw motor currents without commanding motors.")
    parser.add_argument("--port", default="/dev/serial0", help="Serial port to use")
    parser.add_argument("--address", type=parse_address, default=0x80, help="Packet serial address, e.g. 0x80 or 128")
    parser.add_argument("--baud", type=int, default=38400, help="Serial baud rate")
    parser.add_argument("--samples", type=int, default=10, help="Number of current samples to read")
    parser.add_argument("--interval", type=float, default=0.25, help="Seconds between samples")
    args = parser.parse_args()

    print("=== RoboClaw Read Currents ===")
    print("")
    print("This script only reads telemetry. It does not send duty or speed commands.")
    print(f"Connecting to RoboClaw at {args.port} ({args.baud} baud, address 0x{args.address:02X})...")

    try:
        rc = Basicmicro(args.port, args.baud)
        rc.Open()
    except Exception as e:
        print(f"ERROR: Could not connect: {e}")
        print("\nTroubleshooting:")
        print("  - Is the RoboClaw powered on?")
        print(f"  - Is {args.port} available? (ls -l {args.port})")
        print("  - Is gamepad-teleop stopped so it is not holding the serial port?")
        sys.exit(1)

    left_values = []
    right_values = []

    try:
        version = read_version(rc, args.address)
        if not version:
            print("ERROR: RoboClaw did not respond to ReadVersion.")
            print("Check packet serial mode, baud rate, address, wiring, and power.")
            sys.exit(1)
        print(f"RoboClaw version: {version}")

        voltage = read_voltage(rc, args.address)
        if voltage is not None:
            print(f"Main battery: {voltage:.1f}V")

        print("")
        print(" sample   M1 raw   M1 amps   M2 raw   M2 amps")
        print(" ------  -------  --------  -------  --------")

        for sample in range(1, args.samples + 1):
            currents = read_currents(rc, args.address)
            if currents is None:
                print(f"{sample:>7}  read failed")
            else:
                m1_raw, m2_raw, m1_amps, m2_amps = currents
                left_values.append(m1_amps)
                right_values.append(m2_amps)
                print(f"{sample:>7}  {m1_raw:>+7d}  {m1_amps:>+8.2f}  {m2_raw:>+7d}  {m2_amps:>+8.2f}")

            if sample != args.samples:
                time.sleep(args.interval)

        if left_values and right_values:
            print("")
            print_summary("M1", left_values)
            print_summary("M2", right_values)
    finally:
        rc.close()


if __name__ == "__main__":
    main()
