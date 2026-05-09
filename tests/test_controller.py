import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from drivers.controller import ControllerDriver


class FakeDevice:
    def __init__(self, events=(), error=None):
        self.events = events
        self.error = error

    def read_loop(self):
        if self.error is not None:
            raise self.error
        yield from self.events


class ControllerDriverTest(unittest.TestCase):
    def test_reader_normal_exit_signals_disconnect(self):
        driver = ControllerDriver()
        driver.device = FakeDevice()
        driver._running = True
        disconnected = []
        driver._on_disconnect = lambda: disconnected.append(True)

        driver._read_loop()

        self.assertEqual(disconnected, [True])
        self.assertEqual(driver.disconnect_reason, "controller input reader exited")

    def test_reader_exception_signals_disconnect(self):
        driver = ControllerDriver()
        driver.device = FakeDevice(error=RuntimeError("fake failure"))
        driver._running = True
        disconnected = []
        driver._on_disconnect = lambda: disconnected.append(True)

        driver._read_loop()

        self.assertEqual(disconnected, [True])
        self.assertEqual(driver.disconnect_reason, "controller input reader crashed: fake failure")


if __name__ == "__main__":
    unittest.main()
