"""
Motor driver for RoboClaw 2x7A using the basicmicro library.

Uses GPIO UART serial connection for differential drive control.

Hardware setup:
- RoboClaw connected via Pi GPIO UART (GPIO 14 TX, GPIO 15 RX)
- Requires dtoverlay=disable-bt in /boot/firmware/config.txt
- M1 = left motor, M2 = right motor (adjust if wired differently)
- User must be in 'dialout' group: sudo usermod -a -G dialout $USER
"""

from collections.abc import Callable
from typing import Any


def format_version(version):
    if isinstance(version, bytes):
        return version.decode("ascii", errors="replace").strip()
    return str(version).strip()


class MotorDriver:
    """
    Differential drive motor controller using RoboClaw 2x7A.
    
    Speed values range from -1.0 (full reverse) to 1.0 (full forward).
    """
    
    # Duty cycle range for basicmicro library
    MAX_DUTY = 32767
    
    def __init__(
        self,
        port: str = "/dev/serial0",
        address: int = 0x80,
        baud: int = 38400,
        controller_factory: Callable[[str, int], Any] | None = None,
    ):
        """
        Initialize the motor driver.
        
        Args:
            port: Serial port for RoboClaw (USB typically shows as /dev/ttyACM0)
            address: RoboClaw address (default 0x80)
            baud: Baud rate (default 38400)
        """
        self.address = address
        if controller_factory is None:
            from basicmicro import Basicmicro

            controller_factory = Basicmicro

        self.controller = controller_factory(port, baud)
        self.controller.Open()

        result = self.controller.ReadVersion(self.address)
        if not result[0]:
            self.controller.close()
            raise RuntimeError("RoboClaw did not respond to ReadVersion.")
        self.version = format_version(result[1])
        
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
        
        # Convert -1.0..1.0 to duty cycle range
        left_duty = int(left * self.MAX_DUTY)
        right_duty = int(right * self.MAX_DUTY)
        
        # M1 = left, M2 = right
        self.controller.DutyM1(self.address, left_duty)
        self.controller.DutyM2(self.address, right_duty)

    def set_wheel_speeds(self, left_qpps: int, right_qpps: int) -> bool:
        """
        Set closed-loop wheel speed targets in encoder counts per second.

        Args:
            left_qpps: M1 target speed; positive means robot-forward
            right_qpps: M2 target speed; positive means robot-forward
        """
        return bool(self.controller.SpeedM1M2(self.address, int(left_qpps), int(right_qpps)))

    def read_wheel_speeds(self) -> tuple[int | None, int | None]:
        """Read actual closed-loop wheel speeds from RoboClaw encoders."""
        left = self.controller.ReadSpeedM1(self.address)
        right = self.controller.ReadSpeedM2(self.address)
        left_qpps = left[1] if left[0] else None
        right_qpps = right[1] if right[0] else None
        return left_qpps, right_qpps
    
    def stop(self):
        """Immediately stop both motors."""
        self.controller.DutyM1(self.address, 0)
        self.controller.DutyM2(self.address, 0)
    
    def get_battery_voltage(self) -> float | None:
        """Read main battery voltage. Returns None on read failure."""
        result = self.controller.ReadMainBatteryVoltage(self.address)
        if result[0]:
            return result[1] / 10.0
        return None
    
    def get_currents(self) -> tuple[float, float] | None:
        """Read motor currents in amps. Returns (m1, m2) or None on failure."""
        result = self.controller.ReadCurrents(self.address)
        if result[0]:
            # Returns in 10mA units
            return (result[1] / 100.0, result[2] / 100.0)
        return None
    
    def cleanup(self):
        """Stop motors and release resources."""
        self.stop()
        self.controller.close()
