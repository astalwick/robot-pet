#!/usr/bin/env python3
"""
Toggle the DFR0457 MOSFET power controller signal pin.

This proves the Pi can drive the MOSFET control input. The Pi cannot detect the
MOSFET board directly unless a separate feedback wire is added.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/diagnostics/mosfet-power-controller.py
    python scripts/diagnostics/mosfet-power-controller.py --on
    python scripts/diagnostics/mosfet-power-controller.py --off
"""

import argparse
import sys
import time

from gpiozero import OutputDevice


def main():
    parser = argparse.ArgumentParser(description="Test the DFR0457 MOSFET control GPIO.")
    parser.add_argument("--pin", type=int, default=24, help="BCM GPIO number for the MOSFET signal wire")
    parser.add_argument("--on", action="store_true", help="Turn the MOSFET on until Ctrl+C")
    parser.add_argument("--off", action="store_true", help="Turn the MOSFET off and exit")
    parser.add_argument("--cycles", type=int, default=3, help="Number of on/off cycles for the default test")
    parser.add_argument("--seconds", type=float, default=1.0, help="Seconds to hold each on/off state")
    args = parser.parse_args()

    print("=== DFR0457 MOSFET GPIO Test ===")
    print("")
    print(f"Signal pin: GPIO{args.pin}")
    print("Expected wiring: green=GPIO24/pin 18, black=GND/pin 20, red=3.3V/pin 17")
    print("")

    mosfet = OutputDevice(args.pin, active_high=True, initial_value=False)

    try:
        if args.on:
            mosfet.on()
            print("MOSFET signal is ON. Motor rail should be energized if the battery is connected.")
            print("Press Ctrl+C to turn it off.")
            while True:
                time.sleep(1)

        if args.off:
            mosfet.off()
            print("MOSFET signal is OFF. Motor rail should be cut.")
            return

        print("Battery can stay disconnected for this control-side test.")
        print("Use a meter between green and black if you want to see the GPIO switch 0V/3.3V.")
        print("Press Ctrl+C to abort.")
        print("")

        for cycle in range(1, args.cycles + 1):
            print(f"Cycle {cycle}: ON")
            mosfet.on()
            time.sleep(args.seconds)

            print(f"Cycle {cycle}: OFF")
            mosfet.off()
            time.sleep(args.seconds)

        print("")
        print("Done. MOSFET signal is OFF.")
    except KeyboardInterrupt:
        print("\nAborted. MOSFET signal is OFF.")
        sys.exit(1)
    finally:
        mosfet.off()
        mosfet.close()


if __name__ == "__main__":
    main()
