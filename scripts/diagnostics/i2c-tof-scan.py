#!/usr/bin/env python3
"""
Scan a TCA9548A I2C mux for VL53L0X time-of-flight sensors.

Each sensor can share address 0x29 on its own mux channel (no XSHUT wiring).

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/diagnostics/i2c-tof-scan.py
    python scripts/diagnostics/i2c-tof-scan.py --expect 3
"""

import argparse
import sys

if not sys.platform.startswith("linux"):
    print("This script must run on the Pi (Linux + I2C).")
    sys.exit(1)

from smbus2 import SMBus

MUX_DEFAULT = 0x70
TOF_DEFAULT = 0x29
VL53L0X_MODEL_ID = 0xEE
MODEL_ID_REG = 0xC0
REV_ID_REG = 0xC1
NUM_MUX_CHANNELS = 8


def select_mux_channel(bus, mux_address, channel):
    bus.write_byte(mux_address, 1 << channel)


def probe_vl53l0x(bus, tof_address):
    model_id = bus.read_byte_data(tof_address, MODEL_ID_REG)
    if model_id != VL53L0X_MODEL_ID:
        return None
    revision_id = bus.read_byte_data(tof_address, REV_ID_REG)
    return model_id, revision_id


def scan_sensors(bus, mux_address, tof_address):
    try:
        bus.read_byte(mux_address)
    except OSError as error:
        raise RuntimeError(
            f"TCA9548A not responding at 0x{mux_address:02x} "
            f"(is I2C enabled and wiring correct?): {error}"
        ) from error

    found = []
    for channel in range(NUM_MUX_CHANNELS):
        select_mux_channel(bus, mux_address, channel)
        try:
            ids = probe_vl53l0x(bus, tof_address)
        except OSError:
            continue
        if ids is None:
            continue
        model_id, revision_id = ids
        found.append(
            {
                "channel": channel,
                "grove_port": channel + 1,
                "model_id": model_id,
                "revision_id": revision_id,
            }
        )

    return found


def main():
    parser = argparse.ArgumentParser(
        description="Scan TCA9548A mux channels for VL53L0X sensors."
    )
    parser.add_argument("--bus", type=int, default=1, help="I2C bus number (default 1)")
    parser.add_argument(
        "--mux-address",
        type=lambda value: int(value, 0),
        default=MUX_DEFAULT,
        help="TCA9548A address (default 0x70)",
    )
    parser.add_argument(
        "--tof-address",
        type=lambda value: int(value, 0),
        default=TOF_DEFAULT,
        help="VL53L0X address (default 0x29)",
    )
    parser.add_argument(
        "--expect",
        type=int,
        default=None,
        help="Exit with error if this many sensors are not found",
    )
    args = parser.parse_args()

    print("=== I2C ToF scan (TCA9548A + VL53L0X) ===")
    print(f"Bus {args.bus}, mux 0x{args.mux_address:02x}, sensor 0x{args.tof_address:02x}")
    print("")

    with SMBus(args.bus) as bus:
        try:
            sensors = scan_sensors(bus, args.mux_address, args.tof_address)
        except RuntimeError as error:
            print(f"ERROR: {error}")
            sys.exit(1)

    if not sensors:
        print("No VL53L0X sensors found on any mux channel (0–7).")
        print("")
        print("Troubleshooting:")
        print("  - Sensor plugged into a Grove port on the hub?")
        print("  - 3.3V, GND, SDA, SCL connected?")
        print("  - sudo i2cdetect -y 1 shows 0x70, then after")
        print("    sudo i2cset -y 1 0x70 0x01, shows 0x29?")
        sys.exit(1)

    print(f"Found {len(sensors)} VL53L0X sensor(s):")
    for sensor in sensors:
        print(
            f"  channel {sensor['channel']} "
            f"(Grove port {sensor['grove_port']}): "
            f"model=0x{sensor['model_id']:02x} "
            f"rev=0x{sensor['revision_id']:02x}"
        )

    if args.expect is not None and len(sensors) != args.expect:
        print("")
        print(
            f"ERROR: expected {args.expect} sensor(s), found {len(sensors)}."
        )
        sys.exit(1)

    print("")
    print("OK — chip IDs look right. Ranging needs a driver (not in this scan).")


if __name__ == "__main__":
    main()
