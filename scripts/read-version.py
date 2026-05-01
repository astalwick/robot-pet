#!/usr/bin/env python3
"""
Read the RoboClaw firmware version.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/read-version.py
"""

import sys

from basicmicro import Basicmicro


def format_version(version):
    if isinstance(version, bytes):
        return version.decode("ascii", errors="replace").strip()
    return str(version).strip()


def main():
    print("=== RoboClaw Read Version ===")
    print("")

    port = "/dev/serial0"
    address = 0x80
    baud = 38400

    print(f"Connecting to RoboClaw at {port}...")
    try:
        rc = Basicmicro(port, baud)
        rc.Open()
    except Exception as e:
        print(f"ERROR: Could not connect: {e}")
        print("\nTroubleshooting:")
        print("  - Is the RoboClaw powered on?")
        print("  - Is /dev/serial0 available? (ls -l /dev/serial0)")
        print("  - Did you run setup.sh and reboot for UART fix?")
        sys.exit(1)

    try:
        result = rc.ReadVersion(address)
        if result[0]:
            print(f"Version: {format_version(result[1])}")
            return

        print("ERROR: Connected, but could not read version.")
        print(f"  - Port: {port}")
        print(f"  - Address: 0x{address:02X}")
        print(f"  - Baud: {baud}")
        print("  - Check the RoboClaw packet serial address is 0x80.")
        print("  - Check S1/S2 mode and baud rate in Motion Studio.")
        sys.exit(1)
    finally:
        rc.close()


if __name__ == "__main__":
    main()
