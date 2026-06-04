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
    drive_status_message,
    encode_json_line,
    gamepad_update,
    link_loop_message,
    motor_battery_message,
    motor_battery_status,
    motor_rail_update,
    stale_label,
    sensors_update,
    vision_update,
    voice_update,
    wheel_message,
)
from telemetry.socket_client import publish_message, read_telemetry_snapshot, send_voice_command


class TelemetryMessagesTest(unittest.TestCase):
    def test_json_line_round_trip_preserves_none(self):
        message = {"type": "source_update", "value": None}

        decoded = decode_json_line(encode_json_line(message))

        self.assertEqual(decoded, message)

    def test_motor_battery_status_bands(self):
        self.assertEqual(motor_battery_status(None), "unknown")
        self.assertEqual(motor_battery_status(11.1), "ok")
        self.assertEqual(motor_battery_status(10.9), "low")
        self.assertEqual(motor_battery_status(10.8), "critical")

    def test_motor_battery_message_includes_cell_voltage(self):
        self.assertEqual(motor_battery_message(11.7)["cell_voltage"], 3.9)

    def test_motor_rail_update_carries_cutoff_state(self):
        message = motor_rail_update("low_battery_cutoff", 24, 10.7, "low_battery_cutoff", 10.8, 11.1, now=1000.0)

        self.assertEqual(message["type"], "source_update")
        self.assertEqual(message["source"], "motor_rail")
        self.assertEqual(message["time"], 1000.0)
        self.assertEqual(message["state"], "low_battery_cutoff")
        self.assertEqual(message["mosfet_gpio"], 24)
        self.assertEqual(message["last_pack_voltage"], 10.7)

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

    def test_gamepad_update_carries_connection_state(self):
        message = gamepad_update(connected=True, state="driving", now=1000.0)

        self.assertEqual(message["type"], "source_update")
        self.assertEqual(message["source"], "gamepad")
        self.assertEqual(message["time"], 1000.0)
        self.assertTrue(message["connected"])
        self.assertEqual(message["state"], "driving")

    def test_wheel_message_calculates_errors(self):
        message = wheel_message(0.4, 0.2, 100, 50, 90, 55, 1000, 1000, 1.2, 1.1)

        self.assertEqual(message["left_error_qpps"], 10)
        self.assertEqual(message["right_error_qpps"], -5)
        self.assertEqual(message["left_max_qpps"], 1000)

    def test_link_loop_message_includes_health_metrics(self):
        message = link_loop_message(0.8, 2, 0.4, 8.5, 20.0)

        self.assertEqual(message["read_success_rate"], 0.8)
        self.assertEqual(message["consecutive_read_failures"], 2)
        self.assertEqual(message["last_good_read_age_seconds"], 0.4)
        self.assertEqual(message["telemetry_latency_ms"], 8.5)
        self.assertEqual(message["command_loop_hz"], 20.0)

    def test_sensors_update_carries_readings(self):
        message = sensors_update(
            enabled=True,
            status="polling",
            readings=[
                {
                    "name": "cliff_left",
                    "kind": "vl53l0x",
                    "channel": 0,
                    "distance_mm": 120,
                    "ok": True,
                }
            ],
            poll_rate_hz=10.0,
            now=2000.0,
        )

        self.assertEqual(message["source"], "sensors")
        self.assertEqual(message["readings"][0]["distance_mm"], 120)

    def test_vision_update_carries_faces_and_metadata(self):
        message = vision_update(
            enabled=True,
            status="detecting",
            faces=[{"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}],
            image_width=1280,
            image_height=720,
            detection_rate_hz=2.0,
            last_detection_time=1234.5,
            now=2000.0,
        )

        self.assertEqual(message["type"], "source_update")
        self.assertEqual(message["source"], "vision")
        self.assertEqual(message["time"], 2000.0)
        self.assertTrue(message["enabled"])
        self.assertEqual(message["status"], "detecting")
        self.assertEqual(message["faces"][0]["width"], 0.3)
        self.assertEqual(message["image_width"], 1280)
        self.assertEqual(message["image_height"], 720)
        self.assertEqual(message["detection_rate_hz"], 2.0)
        self.assertEqual(message["last_detection_time"], 1234.5)
        self.assertIsNone(message["error"])

    def test_vision_update_carries_error_when_provided(self):
        message = vision_update(
            enabled=True,
            status="error",
            faces=[],
            image_width=None,
            image_height=None,
            detection_rate_hz=2.0,
            last_detection_time=None,
            error="bad config",
            now=1.0,
        )

        self.assertEqual(message["status"], "error")
        self.assertEqual(message["faces"], [])
        self.assertEqual(message["error"], "bad config")

    def test_voice_update_carries_status_and_audio_config(self):
        message = voice_update(
            enabled=True,
            status="listening",
            input_device="hw:0,0",
            output_device="plughw:0,0",
            sample_rate=16000,
            capture_channels=6,
            capture_channel_index=1,
            input_gain=1.2,
            output_gain=0.8,
            assistant_speaking=False,
            partial_transcript="what is",
            last_committed_transcript="what is your name",
            last_assistant_text="I am Bloop.",
            last_error=None,
            barge_in_event_count=2,
            barge_in_last_event="commit: explicit_interrupt",
            now=2000.0,
        )

        self.assertEqual(message["type"], "source_update")
        self.assertEqual(message["source"], "voice")
        self.assertEqual(message["time"], 2000.0)
        self.assertTrue(message["enabled"])
        self.assertEqual(message["status"], "listening")
        self.assertEqual(message["input_device"], "hw:0,0")
        self.assertEqual(message["output_device"], "plughw:0,0")
        self.assertEqual(message["sample_rate"], 16000)
        self.assertEqual(message["capture_channels"], 6)
        self.assertEqual(message["capture_channel_index"], 1)
        self.assertEqual(message["input_gain"], 1.2)
        self.assertEqual(message["output_gain"], 0.8)
        self.assertFalse(message["assistant_speaking"])
        self.assertEqual(message["partial_transcript"], "what is")
        self.assertEqual(message["last_committed_transcript"], "what is your name")
        self.assertEqual(message["last_assistant_text"], "I am Bloop.")
        self.assertIsNone(message["last_error"])
        self.assertEqual(message["barge_in_event_count"], 2)
        self.assertEqual(message["barge_in_last_event"], "commit: explicit_interrupt")

    def test_drive_status_message_includes_command_and_publish_health(self):
        message = drive_status_message("driving", None, True, True, 0, 0.2, 1, False, motion_power_requested=True)

        self.assertEqual(message["state"], "driving")
        self.assertIsNone(message["stop_reason"])
        self.assertTrue(message["controller_reader_alive"])
        self.assertTrue(message["motor_command_ok"])
        self.assertEqual(message["consecutive_motor_command_failures"], 0)
        self.assertEqual(message["last_motor_command_ack_age_seconds"], 0.2)
        self.assertEqual(message["telemetry_publish_failures"], 1)
        self.assertFalse(message["last_telemetry_publish_ok"])
        self.assertTrue(message["motion_power_requested"])


class SocketClientTest(unittest.TestCase):
    def test_read_telemetry_snapshot_reads_initial_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = os.path.join(tmpdir, "sub.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(socket_path)
            server.listen(1)
            snapshot = {"type": "snapshot", "motor_battery": {"status": "ok"}}

            def accept_once():
                conn, _addr = server.accept()
                with conn:
                    conn.sendall(encode_json_line(snapshot))
                server.close()

            thread = threading.Thread(target=accept_once)
            thread.start()

            result = read_telemetry_snapshot(socket_path)
            thread.join(timeout=1.0)

            self.assertEqual(result, snapshot)

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

    def test_send_voice_command_reads_ack_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = os.path.join(tmpdir, "cmd.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(socket_path)
            server.listen(1)
            received = []
            ack = {"ok": True, "accepted": True, "reason": None}

            def accept_once():
                conn, _addr = server.accept()
                with conn:
                    file_obj = conn.makefile("rb")
                    line = file_obj.readline()
                    received.append(line)
                    conn.sendall(encode_json_line(ack))
                server.close()

            thread = threading.Thread(target=accept_once)
            thread.start()

            response = send_voice_command(socket_path, {"cmd": "talk_now"})
            thread.join(timeout=1.0)

            self.assertEqual(decode_json_line(received[0]), {"cmd": "talk_now"})
            self.assertEqual(response, ack)


if __name__ == "__main__":
    unittest.main()
