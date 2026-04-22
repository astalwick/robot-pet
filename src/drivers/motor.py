"""
Motor driver for RoboClaw 2x7A.

Uses the official roboclaw_3 library over USB serial.
Wraps it in a clean interface for differential drive.

Hardware setup:
- RoboClaw connected via USB (shows as /dev/ttyACM0 typically)
- M1 = left motor, M2 = right motor (adjust if wired differently)
"""

from roboclaw_3 import RoboClaw


class MotorDriver:
    """
    Differential drive motor controller using RoboClaw 2x7A.
    
    Speed values range from -1.0 (full reverse) to 1.0 (full forward).
    """
    
    def __init__(
        self,
        port: str = "/dev/ttyACM0",
        address: int = 0x80,
        baud: int = 115200,
    ):
        """
        Initialize the motor driver.
        
        Args:
            port: Serial port for RoboClaw (USB typically shows as /dev/ttyACM0)
            address: RoboClaw address (default 0x80)
            baud: Baud rate (default 115200)
        """
        self.address = address
        self.roboclaw = RoboClaw(port, baud)
        self.roboclaw.Open()
        
        # Stop motors on init
        self.stop()
    
    def set_speed(self, left: float, right: float):
        """
        Set wheel speeds for differential drive.
        
        Args:
            left: Left wheel speed, -1.0 to 1.0
            right: Right wheel speed, -1.0 to 1.0
        """
        left = max(-1.0, min(1.0, left))
        right = max(-1.0, min(1.0, right))
        
        # Convert -1.0..1.0 to 0..127 (backward) or 0..127 (forward)
        # RoboClaw uses 0-127 for speed in each direction
        left_speed = int(abs(left) * 127)
        right_speed = int(abs(right) * 127)
        
        # M1 = left, M2 = right
        if left >= 0:
            self.roboclaw.ForwardM1(self.address, left_speed)
        else:
            self.roboclaw.BackwardM1(self.address, left_speed)
        
        if right >= 0:
            self.roboclaw.ForwardM2(self.address, right_speed)
        else:
            self.roboclaw.BackwardM2(self.address, right_speed)
    
    def stop(self):
        """Immediately stop both motors."""
        self.roboclaw.ForwardM1(self.address, 0)
        self.roboclaw.ForwardM2(self.address, 0)
    
    def get_battery_voltage(self) -> float:
        """Read main battery voltage."""
        return self.roboclaw.ReadMainBatteryVoltage(self.address)[1] / 10.0
    
    def get_currents(self) -> tuple[float, float]:
        """Read motor currents in amps. Returns (m1_current, m2_current)."""
        result = self.roboclaw.ReadCurrents(self.address)
        # Returns in 10mA units
        return (result[1] / 100.0, result[2] / 100.0)
    
    def cleanup(self):
        """Stop motors and release resources."""
        self.stop()
