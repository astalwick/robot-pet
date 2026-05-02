import os
import socket
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from telemetry.messages import (
    controller_message,
    decode_json_line,
    encode_json_line,
    motor_battery_message,
    motor_battery_status,
    stale_label,
    wheel_message,
)
from telemetry.socket_client import publish_message


class TelemetryMessagesTest(unittest.TestCase):
    def test_json_line_round_trip_preserves_none(self):
        message = {"type": "source_update", "value": None}

        decoded = decode_json_line(encode_json_line(message))

        self.assertEqual(decoded, message)

    def test_motor_battery_status_bands(self):
        self.assertEqual(motor_battery_status(None), "unknown")
        self.assertEqual(motor_battery_status(11.1), "ok")
        self.assertEqual(motor_battery_status(10.4), "low")
        self.assertEqual(motor_battery_status(9.6), "critical")

    def test_motor_battery_message_includes_cell_voltage(self):
        self.assertEqual(motor_battery_message(11.7)["cell_voltage"], 3.9)

    def test_stale_label(self):
        self.assertEqual(stale_label(False), "live")
        self.assertEqual(stale_label(True), "stale")
        self.assertEqual(stale_label(True, connected=False), "disconnected")

    def test_controller_message_maps_buttons(self):
        state = SimpleNamespace(
            left_stick_x=0.1,
            left_stick_y=-0.2,
            right_stick_x=0.3,
            right_stick_y=-0.4,
            left_trigger=0.5,
            right_trigger=0.6,
            dpad_x=1,
            dpad_y=-1,
            a=True,
            b=False,
            x=True,
            y=False,
            lb=True,
            rb=False,
            back=False,
            start=True,
            guide=False,
            left_stick_click=True,
            right_stick_click=False,
        )

        message = controller_message(state)

        self.assertTrue(message["buttons"]["a"])
        self.assertTrue(message["buttons"]["left_stick"])
        self.assertEqual(message["right_trigger"], 0.6)

    def test_wheel_message_calculates_errors(self):
        message = wheel_message(0.4, 0.2, 100, 50, 90, 55, 1.2, 1.1)

        self.assertEqual(message["left_error_qpps"], 10)
        self.assertEqual(message["right_error_qpps"], -5)


class SocketClientTest(unittest.TestCase):
    def test_publish_message_sends_one_json_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = os.path.join(tmpdir, "pub.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(socket_path)
            server.listen(1)
            received = []

            def accept_once():
                conn, _addr = server.accept()
                with conn:
                    received.append(conn.recv(1024))
                server.close()

            thread = threading.Thread(target=accept_once)
            thread.start()

            self.assertTrue(publish_message(socket_path, {"type": "test"}))
            thread.join(timeout=1.0)

            self.assertEqual(decode_json_line(received[0]), {"type": "test"})

    def test_publish_message_returns_false_when_unavailable(self):
        self.assertFalse(publish_message("/tmp/robot-pet-missing.sock", {"type": "test"}, timeout=0.01))


if __name__ == "__main__":
    unittest.main()
