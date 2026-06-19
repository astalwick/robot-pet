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
from pathlib import Path

if not sys.platform.startswith("linux"):
    print("This script must run on the Pi (Linux + I2C).")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from smbus2 import SMBus

from drivers.ups_hat_e import GROVE_MUX_ADDRESS, UPS_ADDRESS, UpsHatEDriver


def read_mux_state(bus, address):
    try:
        return bus.read_byte(address)
    except OSError:
        return None


def format_channels(mask):
    channels = [str(channel) for channel in range(8) if mask & (1 << channel)]
    return ", ".join(channels) if channels else "none"


def print_ups(info, mux_state, ups_address, mux_address):
    cells = ", ".join(f"{cell / 1000:.3f}V" for cell in info.cells_mv)
    runtime = "--" if info.runtime_min is None else f"{info.runtime_min}min"
    charge_time = "--" if info.charge_time_min is None else f"{info.charge_time_min}min"

    print(f"UPS HAT E @ 0x{ups_address:02x}")
    print(f"  id: 0x{info.device_id:02x} (expected 0x0a)")
    print(f"  power-on register: 0x{info.power_on_register:02x} (expected 0x0b when idle)")
    print(
        "  charge: "
        f"charging={info.charging} "
        f"fast={info.fast_charging} "
        f"vbus_present={info.vbus_present} "
        f"stage={info.charge_stage}"
    )
    print(
        "  comm: "
        f"bq4050_ok={info.bq4050_ok} "
        f"ip2368_ok={info.ip2368_ok}"
    )
    print(f"  usb-c: {info.vbus_mv / 1000:.3f}V  {info.vbus_ma}mA  {info.vbus_mw / 1000:.3f}W")
    print(f"  battery: {info.battery_mv / 1000:.3f}V  {info.battery_ma}mA  {info.battery_percent}%")
    print(f"  remaining: {info.remaining_mah}mAh  runtime={runtime}  charge_time={charge_time}")
    print(f"  cells: {cells}")
    if mux_state is None:
        print(f"  grove mux @ 0x{mux_address:02x}: not responding")
    else:
        print(
            f"  grove mux @ 0x{mux_address:02x}: "
            f"0x{mux_state:02x} (enabled channels: {format_channels(mux_state)})"
        )

    print("  raw registers:")
    for register, value in sorted(info.raw.items()):
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
            driver = UpsHatEDriver(args.bus, args.address, bus_factory=lambda _bus: bus)
            while True:
                info = driver.read()
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
