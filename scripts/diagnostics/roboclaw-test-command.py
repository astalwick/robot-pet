#!/usr/bin/env python3
"""
Send a real command to the RoboClaw.

By default this sends zero-duty commands to both motor channels. That proves the
Pi can write a motor command without moving the robot.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/diagnostics/roboclaw-test-command.py
    python scripts/diagnostics/roboclaw-test-command.py --pulse
"""

import argparse
import sys
import time

from basicmicro import Basicmicro

MAX_DUTY = 32767


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


def set_duty(rc, address, m1, m2):
    ok_m1 = rc.DutyM1(address, m1)
    ok_m2 = rc.DutyM2(address, m2)
    return ok_m1 and ok_m2


def main():
    parser = argparse.ArgumentParser(description="Send a real RoboClaw command.")
    parser.add_argument("--port", default="/dev/serial0", help="Serial port to use")
    parser.add_argument("--address", type=parse_address, default=0x80, help="Packet serial address, e.g. 0x80 or 128")
    parser.add_argument("--baud", type=int, default=38400, help="Serial baud rate")
    parser.add_argument("--pulse", action="store_true", help="Briefly pulse both motors forward at low duty")
    parser.add_argument("--duty", type=float, default=0.10, help="Pulse duty as a fraction, 0.0 to 1.0")
    parser.add_argument("--duration", type=float, default=0.25, help="Pulse duration in seconds")
    args = parser.parse_args()

    print("=== RoboClaw Command Test ===")
    print("")
    print(f"Connecting to RoboClaw at {args.port} ({args.baud} baud, address 0x{args.address:02X})...")

    try:
        rc = Basicmicro(args.port, args.baud)
        rc.Open()
    except Exception as e:
        print(f"ERROR: Could not connect: {e}")
        sys.exit(1)

    try:
        version = read_version(rc, args.address)
        if not version:
            print("ERROR: RoboClaw did not respond to ReadVersion.")
            print("Check that it is powered on and wired to the Pi UART.")
            sys.exit(1)
        print(f"RoboClaw version: {version}")

        voltage = read_voltage(rc, args.address)
        if voltage:
            print(f"Main battery: {voltage:.1f}V")

        print("Sending stop command: DutyM1=0, DutyM2=0")
        if not set_duty(rc, args.address, 0, 0):
            print("ERROR: Stop command was not acknowledged.")
            sys.exit(1)
        print("Stop command acknowledged.")

        if args.pulse:
            duty = int(args.duty * MAX_DUTY)
            print("")
            print("PULSE MODE: wheels should be off the ground.")
            input("Press Enter to send a short forward pulse, or Ctrl+C to abort...")

            print(f"Sending pulse: DutyM1={duty}, DutyM2={duty} for {args.duration:.2f}s")
            if not set_duty(rc, args.address, duty, duty):
                print("ERROR: Pulse command was not acknowledged.")
                sys.exit(1)

            time.sleep(args.duration)
            set_duty(rc, args.address, 0, 0)
            print("Pulse complete; stop command sent.")

        print("")
        print("Command test passed.")
    except KeyboardInterrupt:
        print("\nAborted; sending stop command.")
        set_duty(rc, args.address, 0, 0)
        sys.exit(1)
    finally:
        rc.close()


if __name__ == "__main__":
    main()
