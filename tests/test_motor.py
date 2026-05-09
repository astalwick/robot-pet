import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from drivers.motor import MotorDriver


class FakeRoboClaw:
    def __init__(self, port, baud):
        self.port = port
        self.baud = baud
        self.calls = []

    def Open(self):
        self.calls.append(("Open",))

    def close(self):
        self.calls.append(("close",))

    def ReadVersion(self, address):
        self.calls.append(("ReadVersion", address))
        return True, b"fake"

    def SetTimeout(self, address, timeout):
        self.calls.append(("SetTimeout", address, timeout))
        return True

    def DutyM1(self, address, duty):
        self.calls.append(("DutyM1", address, duty))

    def DutyM2(self, address, duty):
        self.calls.append(("DutyM2", address, duty))

    def SpeedM1M2(self, address, left_qpps, right_qpps):
        self.calls.append(("SpeedM1M2", address, left_qpps, right_qpps))
        return True

    def ReadSpeedM1(self, address):
        self.calls.append(("ReadSpeedM1", address))
        return True, 123

    def ReadSpeedM2(self, address):
        self.calls.append(("ReadSpeedM2", address))
        return True, -456

    def ReadM1VelocityPID(self, address):
        self.calls.append(("ReadM1VelocityPID", address))
        return True, 1.0, 0.5, 0.25, 11180

    def ReadM2VelocityPID(self, address):
        self.calls.append(("ReadM2VelocityPID", address))
        return True, 1.0, 0.5, 0.25, 11190


class PacketTimeoutError(Exception):
    pass


class TimeoutRoboClaw(FakeRoboClaw):
    def SpeedM1M2(self, address, left_qpps, right_qpps):
        self.calls.append(("SpeedM1M2", address, left_qpps, right_qpps))
        raise PacketTimeoutError("timed out")

    def DutyM1(self, address, duty):
        self.calls.append(("DutyM1", address, duty))
        raise PacketTimeoutError("timed out")


class TimeoutConfigRoboClaw(FakeRoboClaw):
    def SetTimeout(self, address, timeout):
        self.calls.append(("SetTimeout", address, timeout))
        return False


class MotorDriverTest(unittest.TestCase):
    def test_set_wheel_speeds_uses_roboclaw_speed_m1m2(self):
        fake = FakeRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        acknowledged = driver.set_wheel_speeds(100, -50)

        self.assertTrue(acknowledged)
        self.assertIn(("SpeedM1M2", 0x80, 100, -50), fake.calls)

    def test_set_speed_returns_true_after_duty_commands(self):
        fake = FakeRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        acknowledged = driver.set_speed(0.5, -0.5)

        self.assertTrue(acknowledged)
        self.assertIn(("DutyM1", 0x80, 16383), fake.calls)
        self.assertIn(("DutyM2", 0x80, -16383), fake.calls)

    def test_read_wheel_speeds_reads_both_encoders(self):
        fake = FakeRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        self.assertEqual(driver.read_wheel_speeds(), (123, -456))

    def test_read_max_qpps_reads_velocity_pid_caps(self):
        fake = FakeRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        self.assertEqual(driver.read_max_qpps(), (11180, 11190))

    def test_stop_preserves_existing_zero_duty_behavior(self):
        fake = FakeRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        driver.stop()

        self.assertIn(("DutyM1", 0x80, 0), fake.calls)
        self.assertIn(("DutyM2", 0x80, 0), fake.calls)

    def test_configures_roboclaw_serial_timeout_on_init(self):
        fake = FakeRoboClaw("/dev/fake", 38400)

        MotorDriver(serial_timeout=0.7, controller_factory=lambda port, baud: fake)

        self.assertIn(("SetTimeout", 0x80, 0.7), fake.calls)

    def test_init_fails_when_serial_timeout_is_not_acknowledged(self):
        fake = TimeoutConfigRoboClaw("/dev/fake", 38400)

        with self.assertRaises(RuntimeError):
            MotorDriver(controller_factory=lambda port, baud: fake)

        self.assertIn(("close",), fake.calls)

    def test_set_wheel_speeds_returns_false_on_roboclaw_timeout(self):
        fake = TimeoutRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        self.assertFalse(driver.set_wheel_speeds(100, 100))

    def test_set_speed_returns_false_on_roboclaw_timeout(self):
        fake = TimeoutRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        self.assertFalse(driver.set_speed(0.5, 0.5))

    def test_cleanup_ignores_roboclaw_timeout_while_closing(self):
        fake = TimeoutRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        driver.cleanup()

        self.assertIn(("close",), fake.calls)


if __name__ == "__main__":
    unittest.main()
