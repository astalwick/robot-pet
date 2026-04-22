#!/usr/bin/env python3
"""
Motor test script - Phase 0, Step 5.

Run with wheels in the air to verify:
- Both motors spin
- Encoders report movement
- Direction is correct

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/test-motor.py
"""

import sys
import time

sys.path.insert(0, "src")
from drivers.motor import MotorDriver


def test_motor(driver: MotorDriver, name: str, set_speed_func, duration: float = 2.0):
    """Test a single motor direction."""
    print(f"\n{name}...")
    set_speed_func()
    
    for i in range(int(duration * 2)):
        time.sleep(0.5)
        voltage = driver.get_battery_voltage()
        currents = driver.get_currents()
        if voltage and currents:
            print(f"  Battery: {voltage:.1f}V, Currents: M1={currents[0]:.2f}A, M2={currents[1]:.2f}A")
    
    driver.stop()
    print(f"  Stopped.")


def main():
    print("=== Motor Test ===")
    print("Make sure wheels are OFF the ground!")
    print("")
    
    input("Press Enter to begin (Ctrl+C to abort)...")
    
    print("\nConnecting to RoboClaw...")
    try:
        driver = MotorDriver()
    except Exception as e:
        print(f"ERROR: Could not connect to RoboClaw: {e}")
        print("\nTroubleshooting:")
        print("  - Is the RoboClaw powered on?")
        print("  - Is /dev/serial0 available? (ls -l /dev/serial0)")
        print("  - Did you run setup.sh and reboot for UART fix?")
        sys.exit(1)
    
    voltage = driver.get_battery_voltage()
    if voltage:
        print(f"Connected! Battery voltage: {voltage:.1f}V")
    else:
        print("Connected, but could not read battery voltage.")
    
    try:
        # Test each direction
        test_motor(driver, "LEFT MOTOR FORWARD", lambda: driver.set_speed(0.3, 0))
        time.sleep(0.5)
        
        test_motor(driver, "LEFT MOTOR BACKWARD", lambda: driver.set_speed(-0.3, 0))
        time.sleep(0.5)
        
        test_motor(driver, "RIGHT MOTOR FORWARD", lambda: driver.set_speed(0, 0.3))
        time.sleep(0.5)
        
        test_motor(driver, "RIGHT MOTOR BACKWARD", lambda: driver.set_speed(0, -0.3))
        time.sleep(0.5)
        
        test_motor(driver, "BOTH FORWARD (robot would go forward)", lambda: driver.set_speed(0.3, 0.3))
        time.sleep(0.5)
        
        test_motor(driver, "BOTH BACKWARD (robot would go backward)", lambda: driver.set_speed(-0.3, -0.3))
        time.sleep(0.5)
        
        test_motor(driver, "TURN LEFT (left back, right forward)", lambda: driver.set_speed(-0.3, 0.3))
        time.sleep(0.5)
        
        test_motor(driver, "TURN RIGHT (left forward, right back)", lambda: driver.set_speed(0.3, -0.3))
        
        print("\n=== Test Complete ===")
        print("\nIf any direction was wrong, adjust motor direction in RoboClaw config.")
        print("Do NOT swap wires - config is cleaner.")
        
    except KeyboardInterrupt:
        print("\n\nAborted!")
    finally:
        driver.stop()
        driver.cleanup()


if __name__ == "__main__":
    main()
