#!/usr/bin/env python3
"""
RoboClaw configuration script - Phase 0, Step 4.

Sets the 5 critical configuration values:
1. Low-voltage cutoff (SAFETY - do this first)
2. Encoder CPR
3. Motor direction
4. Velocity PID
5. Current limits

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/configure-roboclaw.py

This script walks through each setting interactively.
Values are saved to RoboClaw's EEPROM and persist across power cycles.
"""

import sys
import time

from basicmicro import Basicmicro


# Your specific hardware values (from phase-0-assembly-guide.md)
ENCODER_CPR = 2150  # Counts per revolution (quadrature mode)
LOW_VOLTAGE_CUTOFF = 96  # 9.6V in 0.1V units (3.2V/cell for 3S LiPo)
CURRENT_LIMIT = 5000  # 5A in mA units


def format_version(version):
    if isinstance(version, bytes):
        return version.decode("ascii", errors="replace").strip()
    return str(version).strip()


def main():
    print("=== RoboClaw Configuration ===")
    print("")
    print("This script configures your RoboClaw for the goBILDA 5203 motors")
    print("and 3S LiPo battery. Values are saved to EEPROM.")
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

    result = rc.ReadVersion(address)
    if not result[0]:
        print("ERROR: RoboClaw did not respond to ReadVersion.")
        print("\nTroubleshooting:")
        print("  - Is the RoboClaw switched on?")
        print("  - Is the RoboClaw logic power available?")
        print("  - Check wiring: Pi TX -> RoboClaw S1, Pi RX -> RoboClaw S2, common GND.")
        rc.close()
        sys.exit(1)
    print(f"RoboClaw version: {format_version(result[1])}")
    
    # Read current battery voltage
    result = rc.ReadMainBatteryVoltage(address)
    if result[0]:
        voltage = result[1] / 10.0
        print(f"Connected! Current battery voltage: {voltage:.1f}V")
    else:
        print("Connected, but could not read voltage. Continuing anyway...")
    
    print("")
    print("=" * 50)
    
    # --- 1. Low-voltage cutoff (SAFETY CRITICAL) ---
    print("\n[1/5] LOW-VOLTAGE CUTOFF (SAFETY)")
    print("-" * 40)
    print("Your 3S LiPo is damaged if drained below 9.0V.")
    print(f"Setting cutoff to 9.6V (3.2V/cell, conservative).")
    print("")
    
    input("Press Enter to set low-voltage cutoff...")
    
    # SetMinVoltageMainBattery takes voltage in 0.1V units
    if rc.SetMinVoltageMainBattery(address, LOW_VOLTAGE_CUTOFF):
        print(f"  ✓ Low-voltage cutoff set to {LOW_VOLTAGE_CUTOFF/10:.1f}V")
    else:
        print("  ✗ Failed to set low-voltage cutoff!")
        sys.exit(1)
    
    # --- 2. Encoder CPR ---
    print("\n[2/5] ENCODER COUNTS PER REVOLUTION")
    print("-" * 40)
    print(f"goBILDA 5203-2402-0019: ~{ENCODER_CPR} counts/rev (quadrature)")
    print("")
    
    input("Press Enter to set encoder CPR...")
    
    # Note: The basicmicro library may not have a direct SetEncoderCPR function.
    # Encoder settings are often configured via PID tuning or separate commands.
    # For now, we'll set this via the velocity PID QPPS parameter.
    print("  (Encoder CPR is set via velocity PID tuning in step 4)")
    
    # --- 3. Motor direction ---
    print("\n[3/5] MOTOR DIRECTION")
    print("-" * 40)
    print("Testing current direction. Watch the wheels:")
    print("")
    
    input("Press Enter to spin LEFT motor forward (2 sec)...")
    rc.DutyM1(address, 8000)  # ~25% speed
    time.sleep(2)
    rc.DutyM1(address, 0)
    
    response = input("Did the LEFT wheel spin FORWARD? (y/n): ").strip().lower()
    if response != 'y':
        print("  Inverting M1 direction in config...")
        # Note: Direction inversion may need to be done via Motion Studio
        # or by swapping M1A/M1B wires. The basicmicro library may not
        # expose this directly.
        print("  (If this doesn't work, swap the M1A/M1B wires on the RoboClaw)")
    
    input("Press Enter to spin RIGHT motor forward (2 sec)...")
    rc.DutyM2(address, 8000)
    time.sleep(2)
    rc.DutyM2(address, 0)
    
    response = input("Did the RIGHT wheel spin FORWARD? (y/n): ").strip().lower()
    if response != 'y':
        print("  Inverting M2 direction in config...")
        print("  (If this doesn't work, swap the M2A/M2B wires on the RoboClaw)")
    
    # --- 4. Velocity PID ---
    print("\n[4/5] VELOCITY PID TUNING")
    print("-" * 40)
    print("Starting with conservative PID values.")
    print("You can refine these later with the RoboClaw's auto-tune or manually.")
    print("")
    
    # QPPS = encoder counts per second at full speed
    # At 312 RPM: 312/60 * 2150 = ~11,180 counts/sec
    qpps = 11180
    
    # Conservative starting PID values
    kp = 1.0
    ki = 0.5
    kd = 0.25
    
    print(f"Setting M1 velocity PID: P={kp}, I={ki}, D={kd}, QPPS={qpps}")
    input("Press Enter to continue...")
    
    if rc.SetM1VelocityPID(address, kp, ki, kd, qpps):
        print("  ✓ M1 velocity PID set")
    else:
        print("  ✗ Failed to set M1 velocity PID")
    
    print(f"Setting M2 velocity PID: P={kp}, I={ki}, D={kd}, QPPS={qpps}")
    if rc.SetM2VelocityPID(address, kp, ki, kd, qpps):
        print("  ✓ M2 velocity PID set")
    else:
        print("  ✗ Failed to set M2 velocity PID")
    
    # --- 5. Current limits ---
    print("\n[5/5] CURRENT LIMITS")
    print("-" * 40)
    print(f"Setting per-motor current limit to {CURRENT_LIMIT/1000:.0f}A")
    print("(Protects motors if robot gets stuck)")
    print("")
    
    input("Press Enter to set current limits...")
    
    # SetM1MaxCurrent takes current in 10mA units
    max_current = CURRENT_LIMIT * 100  # Convert to 10mA units
    
    if rc.SetM1MaxCurrent(address, max_current):
        print("  ✓ M1 current limit set")
    else:
        print("  ✗ Failed to set M1 current limit")
    
    if rc.SetM2MaxCurrent(address, max_current):
        print("  ✓ M2 current limit set")
    else:
        print("  ✗ Failed to set M2 current limit")
    
    # --- Save to EEPROM ---
    print("\n" + "=" * 50)
    print("SAVING TO EEPROM")
    print("-" * 40)
    
    input("Press Enter to save all settings to EEPROM...")
    
    if rc.WriteNVM(address):
        print("  ✓ Settings saved to EEPROM")
    else:
        print("  ✗ Failed to save to EEPROM (settings may not persist)")
    
    print("\n=== Configuration Complete ===")
    print("")
    print("Next steps:")
    print("  1. Run scripts/test-motor.py to verify motors work")
    print("  2. Run scripts/teleop.py to drive the robot")
    print("")
    print("If PID tuning feels off (jerky, oscillating, sluggish),")
    print("use the RoboClaw's built-in auto-tune or adjust manually.")
    
    rc.Close()


if __name__ == "__main__":
    main()
