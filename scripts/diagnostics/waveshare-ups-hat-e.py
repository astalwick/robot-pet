#!/usr/bin/env python3
"""
Read Waveshare UPS HAT (E) battery telemetry over I2C.

The UPS HAT (E) lives on the Pi's main I2C bus at 0x2d. Our Grove I2C hub
is a TCA9548A mux at 0x70, so this script also reports the mux state to make
it clear both devices are visible on the same SDA/SCL pins.

This is read-only. Do not write 0x55 to UPS register 0x01 unless you intend to
schedule a power-off.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/diagnostics/waveshare-ups-hat-e.py
    python scripts/diagnostics/waveshare-ups-hat-e.py --watch --interval 2
"""

import argparse
import sys
import time

if not sys.platform.startswith("linux"):
    print("This script must run on the Pi (Linux + I2C).")
    sys.exit(1)

from smbus2 import SMBus

UPS_ADDRESS = 0x2D
GROVE_MUX_ADDRESS = 0x70

REG_ID = 0x00
REG_POWER_ON = 0x01
REG_CHARGE_STATE = 0x02
REG_COMM_STATE = 0x03
REG_USB_C_VBUS_MV = 0x10
REG_USB_C_VBUS_MA = 0x12
REG_USB_C_VBUS_MW = 0x14
REG_BATTERY_MV = 0x20
REG_BATTERY_MA = 0x22
REG_BATTERY_PERCENT = 0x24
REG_BATTERY_REMAINING_MAH = 0x26
REG_BATTERY_RUNTIME_MIN = 0x28
REG_BATTERY_CHARGE_TIME_MIN = 0x2A
REG_CELL1_MV = 0x30
REG_CELL2_MV = 0x32
REG_CELL3_MV = 0x34
REG_CELL4_MV = 0x36

CHARGE_STAGES = {
    0: "standby",
    1: "trickle",
    2: "constant_current",
    3: "constant_voltage",
    4: "charge_wait",
    5: "full",
    6: "charge_timeout",
}


def read_u16(bus, address, register):
    low = bus.read_byte_data(address, register)
    high = bus.read_byte_data(address, register + 1)
    return (high << 8) | low, low, high


def signed_16(value):
    if value & 0x8000:
        return value - 0x10000
    return value


def read_ups(bus, address):
    id_value = bus.read_byte_data(address, REG_ID)
    power_on_value = bus.read_byte_data(address, REG_POWER_ON)
    charge_state = bus.read_byte_data(address, REG_CHARGE_STATE)
    comm_state = bus.read_byte_data(address, REG_COMM_STATE)

    vbus_mv, vbus_mv_low, vbus_mv_high = read_u16(bus, address, REG_USB_C_VBUS_MV)
    vbus_ma, vbus_ma_low, vbus_ma_high = read_u16(bus, address, REG_USB_C_VBUS_MA)
    vbus_mw, vbus_mw_low, vbus_mw_high = read_u16(bus, address, REG_USB_C_VBUS_MW)
    battery_mv, battery_mv_low, battery_mv_high = read_u16(bus, address, REG_BATTERY_MV)
    battery_ma_raw, battery_ma_low, battery_ma_high = read_u16(bus, address, REG_BATTERY_MA)
    battery_percent, battery_percent_low, battery_percent_high = read_u16(bus, address, REG_BATTERY_PERCENT)
    remaining_mah, remaining_mah_low, remaining_mah_high = read_u16(bus, address, REG_BATTERY_REMAINING_MAH)
    runtime_min, runtime_min_low, runtime_min_high = read_u16(bus, address, REG_BATTERY_RUNTIME_MIN)
    charge_time_min, charge_time_min_low, charge_time_min_high = read_u16(bus, address, REG_BATTERY_CHARGE_TIME_MIN)

    cells = []
    raw_cells = []
    for register in (REG_CELL1_MV, REG_CELL2_MV, REG_CELL3_MV, REG_CELL4_MV):
        cell_mv, low, high = read_u16(bus, address, register)
        cells.append(cell_mv)
        raw_cells.append((register, low, high))

    return {
        "id": id_value,
        "power_on": power_on_value,
        "charge_state": charge_state,
        "comm_state": comm_state,
        "vbus_mv": vbus_mv,
        "vbus_ma": signed_16(vbus_ma),
        "vbus_mw": vbus_mw,
        "battery_mv": battery_mv,
        "battery_ma": signed_16(battery_ma_raw),
        "battery_percent": battery_percent,
        "remaining_mah": remaining_mah,
        "runtime_min": runtime_min,
        "charge_time_min": charge_time_min,
        "cells_mv": cells,
        "raw": {
            REG_ID: id_value,
            REG_POWER_ON: power_on_value,
            REG_CHARGE_STATE: charge_state,
            REG_COMM_STATE: comm_state,
            REG_USB_C_VBUS_MV: vbus_mv_low,
            REG_USB_C_VBUS_MV + 1: vbus_mv_high,
            REG_USB_C_VBUS_MA: vbus_ma_low,
            REG_USB_C_VBUS_MA + 1: vbus_ma_high,
            REG_USB_C_VBUS_MW: vbus_mw_low,
            REG_USB_C_VBUS_MW + 1: vbus_mw_high,
            REG_BATTERY_MV: battery_mv_low,
            REG_BATTERY_MV + 1: battery_mv_high,
            REG_BATTERY_MA: battery_ma_low,
            REG_BATTERY_MA + 1: battery_ma_high,
            REG_BATTERY_PERCENT: battery_percent_low,
            REG_BATTERY_PERCENT + 1: battery_percent_high,
            REG_BATTERY_REMAINING_MAH: remaining_mah_low,
            REG_BATTERY_REMAINING_MAH + 1: remaining_mah_high,
            REG_BATTERY_RUNTIME_MIN: runtime_min_low,
            REG_BATTERY_RUNTIME_MIN + 1: runtime_min_high,
            REG_BATTERY_CHARGE_TIME_MIN: charge_time_min_low,
            REG_BATTERY_CHARGE_TIME_MIN + 1: charge_time_min_high,
            **{register: low for register, low, _high in raw_cells},
            **{register + 1: high for register, _low, high in raw_cells},
        },
    }


def read_mux_state(bus, address):
    try:
        return bus.read_byte(address)
    except OSError:
        return None


def format_channels(mask):
    channels = [str(channel) for channel in range(8) if mask & (1 << channel)]
    return ", ".join(channels) if channels else "none"


def print_ups(info, mux_state, ups_address, mux_address):
    charge_state = info["charge_state"]
    comm_state = info["comm_state"]
    stage = CHARGE_STAGES.get(charge_state & 0x07, "unknown")
    cells = ", ".join(f"{cell / 1000:.3f}V" for cell in info["cells_mv"])
    runtime = "--" if info["runtime_min"] == 0xFFFF else f"{info['runtime_min']}min"
    charge_time = "--" if info["charge_time_min"] == 0xFFFF else f"{info['charge_time_min']}min"

    print(f"UPS HAT E @ 0x{ups_address:02x}")
    print(f"  id: 0x{info['id']:02x} (expected 0x0a)")
    print(f"  power-on register: 0x{info['power_on']:02x} (expected 0x0b when idle)")
    print(
        "  charge: "
        f"charging={bool(charge_state & 0x80)} "
        f"fast={bool(charge_state & 0x40)} "
        f"vbus_present={bool(charge_state & 0x20)} "
        f"stage={stage}"
    )
    print(
        "  comm: "
        f"bq4050_ok={bool(comm_state & 0x02)} "
        f"ip2368_ok={bool(comm_state & 0x01)}"
    )
    print(f"  usb-c: {info['vbus_mv'] / 1000:.3f}V  {info['vbus_ma']}mA  {info['vbus_mw'] / 1000:.3f}W")
    print(f"  battery: {info['battery_mv'] / 1000:.3f}V  {info['battery_ma']}mA  {info['battery_percent']}%")
    print(f"  remaining: {info['remaining_mah']}mAh  runtime={runtime}  charge_time={charge_time}")
    print(f"  cells: {cells}")
    if mux_state is None:
        print(f"  grove mux @ 0x{mux_address:02x}: not responding")
    else:
        print(
            f"  grove mux @ 0x{mux_address:02x}: "
            f"0x{mux_state:02x} (enabled channels: {format_channels(mux_state)})"
        )

    print("  raw registers:")
    for register, value in sorted(info["raw"].items()):
        print(f"    0x{register:02x}: 0x{value:02x}")


def main():
    parser = argparse.ArgumentParser(description="Read Waveshare UPS HAT (E) I2C telemetry.")
    parser.add_argument("--bus", type=int, default=1, help="I2C bus number (default 1)")
    parser.add_argument("--address", type=lambda value: int(value, 0), default=UPS_ADDRESS, help="UPS I2C address (default 0x2d)")
    parser.add_argument("--mux-address", type=lambda value: int(value, 0), default=GROVE_MUX_ADDRESS, help="Grove/TCA9548A address (default 0x70)")
    parser.add_argument("--watch", action="store_true", help="Keep reading until Ctrl-C")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between reads in --watch mode")
    args = parser.parse_args()

    print("=== Waveshare UPS HAT (E) diagnostic ===")
    print(f"Bus {args.bus}, UPS 0x{args.address:02x}, Grove mux 0x{args.mux_address:02x}")
    print("")

    try:
        with SMBus(args.bus) as bus:
            while True:
                info = read_ups(bus, args.address)
                mux_state = read_mux_state(bus, args.mux_address)
                print_ups(info, mux_state, args.address, args.mux_address)
                if not args.watch:
                    break
                print("")
                time.sleep(args.interval)
    except OSError as error:
        print(f"ERROR: UPS HAT (E) did not respond at 0x{args.address:02x}: {error}")
        print("")
        print("Troubleshooting:")
        print("  - Is I2C enabled on the Pi?")
        print("  - Does `sudo i2cdetect -y 1` show 0x2d?")
        print("  - Is the HAT firmly seated on the Pi header/pogo pins?")
        print("  - Is another process holding the bus in a long transaction?")
        sys.exit(1)
    except KeyboardInterrupt:
        print("")
        print("Stopped.")


if __name__ == "__main__":
    main()
