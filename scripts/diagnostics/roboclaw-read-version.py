#!/usr/bin/env python3
"""
Read the RoboClaw firmware version.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/diagnostics/roboclaw-read-version.py
    python scripts/diagnostics/roboclaw-read-version.py --baud 2400
    python scripts/diagnostics/roboclaw-read-version.py --scan
"""

import argparse
import sys

from basicmicro import Basicmicro

COMMON_BAUDS = [2400, 9600, 19200, 38400, 57600, 115200]


def format_version(version):
    if isinstance(version, bytes):
        return version.decode("ascii", errors="replace").strip()
    return str(version).strip()


def parse_address(value):
    return int(value, 0)


def read_version(port, address, baud, verbose=True):
    if verbose:
        print(f"Connecting to RoboClaw at {port} ({baud} baud, address 0x{address:02X})...")

    rc = Basicmicro(port, baud)
    rc.Open()
    try:
        result = rc.ReadVersion(address)
        if result[0]:
            return format_version(result[1])
        return None
    finally:
        rc.close()


def main():
    parser = argparse.ArgumentParser(description="Read the RoboClaw firmware version.")
    parser.add_argument("--port", default="/dev/serial0", help="Serial port to use")
    parser.add_argument("--address", type=parse_address, default=0x80, help="Packet serial address, e.g. 0x80 or 128")
    parser.add_argument("--baud", type=int, default=38400, help="Serial baud rate")
    parser.add_argument("--scan", action="store_true", help="Try common RoboClaw baud rates")
    args = parser.parse_args()

    print("=== RoboClaw Read Version ===")
    print("")

    bauds = COMMON_BAUDS if args.scan else [args.baud]

    for baud in bauds:
        try:
            version = read_version(args.port, args.address, baud)
        except Exception as e:
            print(f"ERROR: Could not connect: {e}")
            print("\nTroubleshooting:")
            print("  - Is the RoboClaw powered on?")
            print(f"  - Is {args.port} available? (ls -l {args.port})")
            print("  - Did you run setup.sh and reboot for UART fix?")
            sys.exit(1)

        if version:
            print(f"Version: {version}")
            print(f"Matched baud: {baud}")
            return

        print(f"No response at {baud} baud.")

    print("\nERROR: Connected, but could not read version.")
    print(f"  - Port: {args.port}")
    print(f"  - Address: 0x{args.address:02X}")
    print(f"  - Baud(s): {', '.join(str(baud) for baud in bauds)}")
    print("  - Check the RoboClaw packet serial address is 0x80 / 128.")
    print("  - Check S1/S2 mode and baud rate in Motion Studio.")
    print("  - Check wiring: Pi TX -> RoboClaw S1, Pi RX -> RoboClaw S2, common GND.")
    sys.exit(1)


if __name__ == "__main__":
    main()
