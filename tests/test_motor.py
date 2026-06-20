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

    def GetEncoders(self, address):
        self.calls.append(("GetEncoders", address))
        return True, 1000, 2000

    def ReadM1VelocityPID(self, address):
        self.calls.append(("ReadM1VelocityPID", address))
        return True, 1.0, 0.5, 0.25, 11180

    def ReadM2VelocityPID(self, address):
        self.calls.append(("ReadM2VelocityPID", address))
        return True, 1.0, 0.5, 0.25, 11190

    def ReadMainBatteryVoltage(self, address):
        self.calls.append(("ReadMainBatteryVoltage", address))
        return True, 123

    def ReadCurrents(self, address):
        self.calls.append(("ReadCurrents", address))
        return True, 150, 175


class PacketTimeoutError(Exception):
    pass


class SerialException(Exception):
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


class SerialErrorRoboClaw(FakeRoboClaw):
    def SpeedM1M2(self, address, left_qpps, right_qpps):
        self.calls.append(("SpeedM1M2", address, left_qpps, right_qpps))
        raise SerialException("serial link dropped")


class ReadTimeoutRoboClaw(FakeRoboClaw):
    def ReadSpeedM1(self, address):
        self.calls.append(("ReadSpeedM1", address))
        raise PacketTimeoutError("timed out")

    def GetEncoders(self, address):
        self.calls.append(("GetEncoders", address))
        raise PacketTimeoutError("timed out")

    def ReadM1VelocityPID(self, address):
        self.calls.append(("ReadM1VelocityPID", address))
        raise PacketTimeoutError("timed out")

    def ReadMainBatteryVoltage(self, address):
        self.calls.append(("ReadMainBatteryVoltage", address))
        raise PacketTimeoutError("timed out")

    def ReadCurrents(self, address):
        self.calls.append(("ReadCurrents", address))
        raise PacketTimeoutError("timed out")


class NonRecoverableReadRoboClaw(FakeRoboClaw):
    def ReadSpeedM1(self, address):
        self.calls.append(("ReadSpeedM1", address))
        raise RuntimeError("unexpected read failure")

    def GetEncoders(self, address):
        self.calls.append(("GetEncoders", address))
        raise RuntimeError("unexpected read failure")


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

    def test_read_wheel_positions_reads_both_encoders(self):
        fake = FakeRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        self.assertEqual(driver.read_wheel_positions(), (1000, 2000))

    def test_read_wheel_positions_returns_none_on_roboclaw_timeout(self):
        fake = ReadTimeoutRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        self.assertEqual(driver.read_wheel_positions(), (None, None))

    def test_read_wheel_positions_reraises_unexpected_error(self):
        fake = NonRecoverableReadRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        with self.assertRaises(RuntimeError):
            driver.read_wheel_positions()

    def test_read_max_qpps_reads_velocity_pid_caps(self):
        fake = FakeRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        self.assertEqual(driver.read_max_qpps(), (11180, 11190))

    def test_get_battery_voltage_reads_main_pack_voltage(self):
        fake = FakeRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        self.assertEqual(driver.get_battery_voltage(), 12.3)

    def test_get_currents_reads_motor_currents(self):
        fake = FakeRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        self.assertEqual(driver.get_currents(), (1.5, 1.75))

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

    def test_set_wheel_speeds_returns_false_on_serial_error(self):
        fake = SerialErrorRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        self.assertFalse(driver.set_wheel_speeds(100, 100))

    def test_set_speed_returns_false_on_roboclaw_timeout(self):
        fake = TimeoutRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        self.assertFalse(driver.set_speed(0.5, 0.5))

    def test_read_wheel_speeds_returns_none_on_roboclaw_timeout(self):
        fake = ReadTimeoutRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        self.assertEqual(driver.read_wheel_speeds(), (None, None))

    def test_read_max_qpps_returns_none_on_roboclaw_timeout(self):
        fake = ReadTimeoutRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        self.assertEqual(driver.read_max_qpps(), (None, None))

    def test_get_battery_voltage_returns_none_on_roboclaw_timeout(self):
        fake = ReadTimeoutRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        self.assertIsNone(driver.get_battery_voltage())

    def test_get_currents_returns_none_on_roboclaw_timeout(self):
        fake = ReadTimeoutRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        self.assertIsNone(driver.get_currents())

    def test_read_wheel_speeds_reraises_unexpected_error(self):
        fake = NonRecoverableReadRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        with self.assertRaises(RuntimeError):
            driver.read_wheel_speeds()

    def test_cleanup_ignores_roboclaw_timeout_while_closing(self):
        fake = TimeoutRoboClaw("/dev/fake", 38400)
        driver = MotorDriver(controller_factory=lambda port, baud: fake)

        driver.cleanup()

        self.assertIn(("close",), fake.calls)


if __name__ == "__main__":
    unittest.main()
