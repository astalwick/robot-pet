#!/usr/bin/env python3
"""
Keyboard teleoperation - Phase 0, Step 6.

Drive the robot with WASD keys over SSH.

Controls:
    W / Up      - Forward
    S / Down    - Backward
    A / Left    - Turn left
    D / Right   - Turn right
    Space       - Stop
    Q           - Quit
    +/-         - Increase/decrease speed

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/teleop.py
"""

import curses
import sys
import time

sys.path.insert(0, "src")
from drivers.motor import MotorDriver


class Teleop:
    def __init__(self, driver: MotorDriver):
        self.driver = driver
        self.speed = 0.3  # Default speed (0.0 to 1.0)
        self.speed_step = 0.1
        self.min_speed = 0.1
        self.max_speed = 0.8
        
        # Current commanded velocities
        self.left = 0.0
        self.right = 0.0
    
    def set_motion(self, left: float, right: float):
        """Set motor velocities."""
        self.left = left * self.speed
        self.right = right * self.speed
        self.driver.set_speed(self.left, self.right)
    
    def stop(self):
        """Stop all motion."""
        self.left = 0.0
        self.right = 0.0
        self.driver.stop()
    
    def increase_speed(self):
        """Increase speed setting."""
        self.speed = min(self.max_speed, self.speed + self.speed_step)
    
    def decrease_speed(self):
        """Decrease speed setting."""
        self.speed = max(self.min_speed, self.speed - self.speed_step)
    
    def get_status(self) -> str:
        """Get current status string."""
        voltage = self.driver.get_battery_voltage()
        currents = self.driver.get_currents()
        
        status = f"Speed: {self.speed:.1f} | L: {self.left:+.2f} R: {self.right:+.2f}"
        if voltage:
            status += f" | Battery: {voltage:.1f}V"
        if currents:
            status += f" | Current: {currents[0]:.1f}A / {currents[1]:.1f}A"
        return status


def run_teleop(stdscr, driver: MotorDriver):
    """Main teleop loop using curses."""
    teleop = Teleop(driver)
    
    # Configure curses
    curses.curs_set(0)  # Hide cursor
    stdscr.nodelay(True)  # Non-blocking input
    stdscr.timeout(100)  # 100ms refresh
    
    # Key mappings
    FORWARD = {ord('w'), ord('W'), curses.KEY_UP}
    BACKWARD = {ord('s'), ord('S'), curses.KEY_DOWN}
    LEFT = {ord('a'), ord('A'), curses.KEY_LEFT}
    RIGHT = {ord('d'), ord('D'), curses.KEY_RIGHT}
    STOP = {ord(' ')}
    QUIT = {ord('q'), ord('Q')}
    FASTER = {ord('+'), ord('=')}
    SLOWER = {ord('-'), ord('_')}
    
    running = True
    last_key_time = time.time()
    auto_stop_delay = 0.3  # Stop if no key pressed for this long
    
    while running:
        stdscr.clear()
        
        # Draw header
        stdscr.addstr(0, 0, "=== Robot Teleop ===", curses.A_BOLD)
        stdscr.addstr(2, 0, "Controls:")
        stdscr.addstr(3, 0, "  W/Up    - Forward")
        stdscr.addstr(4, 0, "  S/Down  - Backward")
        stdscr.addstr(5, 0, "  A/Left  - Turn left")
        stdscr.addstr(6, 0, "  D/Right - Turn right")
        stdscr.addstr(7, 0, "  Space   - Stop")
        stdscr.addstr(8, 0, "  +/-     - Speed up/down")
        stdscr.addstr(9, 0, "  Q       - Quit")
        
        # Draw status
        stdscr.addstr(11, 0, teleop.get_status())
        
        stdscr.refresh()
        
        # Handle input
        try:
            key = stdscr.getch()
        except:
            key = -1
        
        if key != -1:
            last_key_time = time.time()
            
            if key in QUIT:
                running = False
            elif key in FORWARD:
                teleop.set_motion(1, 1)
            elif key in BACKWARD:
                teleop.set_motion(-1, -1)
            elif key in LEFT:
                teleop.set_motion(-1, 1)
            elif key in RIGHT:
                teleop.set_motion(1, -1)
            elif key in STOP:
                teleop.stop()
            elif key in FASTER:
                teleop.increase_speed()
            elif key in SLOWER:
                teleop.decrease_speed()
        
        # Auto-stop if no recent key press
        if time.time() - last_key_time > auto_stop_delay:
            if teleop.left != 0 or teleop.right != 0:
                teleop.stop()
    
    teleop.stop()


def main():
    print("=== Robot Teleop ===")
    print("")
    
    print("Connecting to RoboClaw...")
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
        print("Connected (could not read battery voltage)")
    
    print("\nStarting teleop interface...")
    print("(If display looks broken, resize your terminal)")
    time.sleep(1)
    
    try:
        curses.wrapper(lambda stdscr: run_teleop(stdscr, driver))
    except KeyboardInterrupt:
        pass
    finally:
        driver.stop()
        driver.cleanup()
        print("\nTeleop ended. Motors stopped.")


if __name__ == "__main__":
    main()
