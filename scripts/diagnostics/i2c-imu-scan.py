#!/usr/bin/env python3
"""
Check for a BNO085 IMU on the TCA9548A mux.

Default: channel 3 (Grove port 4). Stop robot-sensors first so nothing else
owns the I2C bus.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/diagnostics/i2c-imu-scan.py
    python scripts/diagnostics/i2c-imu-scan.py --channel 3
    python scripts/diagnostics/i2c-imu-scan.py --scan-all
    python scripts/diagnostics/i2c-imu-scan.py --no-read
"""

import argparse
import sys
import time

if not sys.platform.startswith("linux"):
    print("This script must run on the Pi (Linux + I2C).")
    sys.exit(1)

from smbus2 import SMBus

MUX_DEFAULT = 0x70
NUM_MUX_CHANNELS = 8
BNO085_ADDRESSES = (0x4A, 0x4B)


def select_mux_channel(bus, mux_address, channel):
    bus.write_byte(mux_address, 1 << channel)


def probe_address(bus, address):
    try:
        bus.write_quick(address)
    except OSError:
        return False
    return True


def find_imu(bus, mux_address, channel):
    select_mux_channel(bus, mux_address, channel)
    for address in BNO085_ADDRESSES:
        if probe_address(bus, address):
            return address
    return None


def read_acceleration(channel, address, mux_address):
    try:
        import board
        import adafruit_tca9548a
        from adafruit_bno08x.i2c import BNO08X_I2C
    except ImportError as error:
        raise RuntimeError(
            "adafruit-circuitpython-bno08x not installed "
            "(run: pip install -e . from the repo venv on the Pi)"
        ) from error

    i2c = board.I2C()
    mux = adafruit_tca9548a.TCA9548A(i2c, address=mux_address)
    imu = BNO08X_I2C(mux[channel], address=address)

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        accel = imu.acceleration
        if accel is not None:
            return accel
        time.sleep(0.05)

    raise RuntimeError("BNO085 responded on I2C but no acceleration report arrived")


def main():
    parser = argparse.ArgumentParser(
        description="Check for BNO085 on a TCA9548A mux channel."
    )
    parser.add_argument("--bus", type=int, default=1, help="I2C bus number (default 1)")
    parser.add_argument(
        "--mux-address",
        type=lambda value: int(value, 0),
        default=MUX_DEFAULT,
        help="TCA9548A address (default 0x70)",
    )
    parser.add_argument(
        "--channel",
        type=int,
        default=3,
        help="Mux channel to check (default 3 = Grove port 4)",
    )
    parser.add_argument(
        "--scan-all",
        action="store_true",
        help="Scan mux channels 0–7 for a BNO085",
    )
    parser.add_argument(
        "--no-read",
        action="store_true",
        help="Only check I2C presence, skip fusion read",
    )
    args = parser.parse_args()

    print("=== I2C IMU scan (TCA9548A + BNO085) ===")
    print(f"Bus {args.bus}, mux 0x{args.mux_address:02x}")
    print("")

    with SMBus(args.bus) as bus:
        try:
            bus.read_byte(args.mux_address)
        except OSError as error:
            print(f"ERROR: TCA9548A not responding at 0x{args.mux_address:02x}: {error}")
            sys.exit(1)

        if args.scan_all:
            found = []
            for channel in range(NUM_MUX_CHANNELS):
                address = find_imu(bus, args.mux_address, channel)
                if address is not None:
                    found.append((channel, address))
        else:
            address = find_imu(bus, args.mux_address, args.channel)
            found = [(args.channel, address)] if address is not None else []

    if not found:
        if args.scan_all:
            print("No BNO085 found on mux channels 0–7.")
        else:
            print(
                f"No BNO085 found on channel {args.channel} "
                f"(Grove port {args.channel + 1})."
            )
        print("")
        print("Troubleshooting:")
        print("  - IMU plugged into the Grove hub?")
        print("  - Try: sudo i2cset -y 1 0x70", hex(1 << args.channel), "&& sudo i2cdetect -y 1")
        print("  - Expect 0x4a (or 0x4b if SA0 is high)")
        sys.exit(1)

    for channel, address in found:
        print(
            f"Found BNO085 on channel {channel} "
            f"(Grove port {channel + 1}) at 0x{address:02x}"
        )

    if args.no_read:
        print("")
        print("OK — chip ACKed on I2C.")
        return

    channel, address = found[0]
    print("")
    print("Reading acceleration...")
    try:
        accel = read_acceleration(channel, address, args.mux_address)
    except RuntimeError as error:
        print(f"ERROR: {error}")
        sys.exit(1)

    print(f"acceleration (m/s²): x={accel[0]:.2f}  y={accel[1]:.2f}  z={accel[2]:.2f}")
    print("")
    print("OK — BNO085 is present and reporting fusion data.")


if __name__ == "__main__":
    main()
