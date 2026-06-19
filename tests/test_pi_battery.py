import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from drivers.ups_hat_e import UpsHatEDriver
from robot_pi_battery import PiBatteryConfig, PiBatteryService


class FakeBus:
    def __init__(self, _bus=1):
        self.closed = False
        self.registers = {
            0x00: 0x0A,
            0x01: 0x0B,
            0x02: 0x00,
            0x03: 0x02,
            0x10: 0x00,
            0x11: 0x00,
            0x12: 0x00,
            0x13: 0x00,
            0x14: 0x00,
            0x15: 0x00,
            0x20: 0x96,
            0x21: 0x3E,
            0x22: 0xA2,
            0x23: 0xFE,
            0x24: 0x49,
            0x25: 0x00,
            0x26: 0x87,
            0x27: 0x0D,
            0x28: 0x49,
            0x29: 0x02,
            0x2A: 0xFF,
            0x2B: 0xFF,
            0x30: 0xA5,
            0x31: 0x0F,
            0x32: 0xA3,
            0x33: 0x0F,
            0x34: 0xA8,
            0x35: 0x0F,
            0x36: 0xA8,
            0x37: 0x0F,
        }

    def read_byte_data(self, _address, register):
        return self.registers[register]

    def close(self):
        self.closed = True


class UpsHatEDriverTest(unittest.TestCase):
    def test_driver_decodes_battery_registers(self):
        driver = UpsHatEDriver(bus_factory=FakeBus)

        reading = driver.read()

        self.assertEqual(reading.device_id, 0x0A)
        self.assertEqual(reading.battery_mv, 16022)
        self.assertEqual(reading.battery_ma, -350)
        self.assertEqual(reading.battery_percent, 73)
        self.assertEqual(reading.runtime_min, 585)
        self.assertIsNone(reading.charge_time_min)
        self.assertEqual(reading.cells_mv, (4005, 4003, 4008, 4008))
        self.assertTrue(reading.bq4050_ok)
        self.assertFalse(reading.ip2368_ok)


class PiBatteryServiceTest(unittest.TestCase):
    def test_tick_publishes_pi_battery_update(self):
        published = []
        service = PiBatteryService(
            PiBatteryConfig(),
            publish=published.append,
            driver_factory=lambda: UpsHatEDriver(bus_factory=FakeBus),
        )

        service.tick()

        self.assertEqual(published[-1]["source"], "pi_battery")
        self.assertEqual(published[-1]["pack_voltage"], 16.022)
        self.assertEqual(published[-1]["percent"], 73)
        self.assertEqual(published[-1]["power_state"], "discharging")


if __name__ == "__main__":
    unittest.main()
