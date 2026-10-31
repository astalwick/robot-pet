#!/usr/bin/env python3
"""ROS lidar status node: watches the apt rplidar driver for the dashboard, and
turns the lidar on and off with the motor board.

The driver (robot-lidar.service) only speaks ROS, so this node listens to its
/scan and /diagnostics, asks for its lifecycle state (active means the lidar is
on), and twice a second sends a lidar_update to the hub, alongside whether the
lidar and nav services are running.

It also reads robot-motion's drive status from the hub: when the RoboClaw
starts answering (so the rail and the wall switch are on) the lidar is
activated, and a few seconds after it stops answering the lidar is
deactivated. The lidar is USB powered, so following the rail flag alone would
spin it up with the wall switch off. Only the edges matter, so the dashboard's
manual on/off holds until the board next changes.

The A1's motor spins from the moment the driver opens its serial port (on
configure), and only a deactivate stops it. So a driver that starts with the
board off (a redeploy, a reboot, the lidar plugged back in) is turned on and
straight back off, once.
"""

import argparse
import os
import subprocess
import threading
import time
from collections import deque

import rclpy
from diagnostic_msgs.msg import DiagnosticArray
from lifecycle_msgs.msg import Transition
from lifecycle_msgs.srv import ChangeState, GetState
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import LaserScan

from lib.log import setup_logging
from telemetry.messages import lidar_update
from telemetry.paths import DEFAULT_PUBLISH_SOCKET, DEFAULT_SUBSCRIBE_SOCKET
from telemetry.socket_client import publish_message, subscribe

log = setup_logging("robot-lidar-status")

# The udev rule symlinks the A1 here when it is plugged in.
LIDAR_DEVICE = "/dev/rplidar"

# robot-battery already holds the rail 5 s after motion stops; this keeps a
# bouncing board from spinning the lidar up and down with it.
BOARD_OFF_DEBOUNCE_SECONDS = 3.0


def board_is_answering(snapshot: dict) -> bool:
    source = (snapshot.get("sources") or {}).get("robot_motion") or {}
    if source.get("stale") is not False:
        return False
    drive_status = snapshot.get("drive_status") or {}
    return drive_status.get("roboclaw_ready") is True


class LidarStatusNode(Node):
    def __init__(self, telemetry_socket: str, subscribe_socket: str) -> None:
        super().__init__("robot_lidar_status")
        self.telemetry_socket = telemetry_socket
        self.subscribe_socket = subscribe_socket
        # receive times of the last few scans, for the scan rate
        self.scan_times: deque[float] = deque(maxlen=20)
        self.points: int | None = None
        self.valid_points: int | None = None
        self.diagnostics: list[dict] = []
        self.diagnostics_time: float | None = None
        self.lidar_active = False
        # the driver's lifecycle state ("unconfigured", "inactive", "active"), or None
        self.driver_state: str | None = None
        # written by the hub thread, read by the timer
        self.board_on = False
        # the board state the timer last acted on; None until the first tick
        self.board_seen_on: bool | None = None
        self.board_off_since: float | None = None
        self.driver_ready = False
        # a just-started driver is being turned on only to be turned off
        self.spinning_down = False
        self.state_client = self.create_client(GetState, "/rplidar_node/get_state")
        self.change_state_client = self.create_client(ChangeState, "/rplidar_node/change_state")
        self.create_subscription(LaserScan, "/scan", self.on_scan, qos_profile_sensor_data)
        self.create_subscription(DiagnosticArray, "/diagnostics", self.on_diagnostics, 10)
        self.create_timer(0.5, self.publish_status)
        threading.Thread(target=self.watch_board, daemon=True).start()

    def watch_board(self) -> None:
        for snapshot in subscribe(self.subscribe_socket):
            self.board_on = board_is_answering(snapshot)

    def on_scan(self, scan: LaserScan) -> None:
        self.scan_times.append(time.monotonic())
        self.points = len(scan.ranges)
        self.valid_points = sum(1 for distance in scan.ranges if scan.range_min <= distance <= scan.range_max)

    def on_diagnostics(self, array: DiagnosticArray) -> None:
        # twist_mux publishes on /diagnostics too; keep only the driver's own.
        lidar_statuses = [status for status in array.status if status.name.startswith("rplidar_node")]
        if not lidar_statuses:
            return
        self.diagnostics = [
            {
                "name": status.name,
                "level": ord(status.level),
                "message": status.message,
                "values": {pair.key: pair.value for pair in status.values},
            }
            for status in lidar_statuses
        ]
        self.diagnostics_time = time.monotonic()

    def on_driver_state(self, future) -> None:
        self.driver_state = future.result().current_state.label
        self.lidar_active = self.driver_state == "active"

    def follow_board(self) -> None:
        # a driver still configuring cannot be turned on yet
        if not self.state_client.service_is_ready() or self.driver_state not in ("inactive", "active"):
            self.driver_ready = False
            return
        # A driver that just (re)started comes up inactive, so apply the board state once.
        driver_appeared = not self.driver_ready
        self.driver_ready = True
        board_on = self.board_on
        now = time.monotonic()
        if board_on:
            self.board_off_since = None
            self.spinning_down = False
            if driver_appeared or self.board_seen_on is not True:
                self.set_lidar(True)
        elif driver_appeared:
            log.info("lidar driver started with the motor board off: turning it on and off to stop the motor")
            self.spinning_down = True
            self.set_lidar(True)
        elif self.spinning_down and self.lidar_active:
            self.spinning_down = False
            self.set_lidar(False)
        elif self.board_seen_on is True:
            self.board_off_since = now
        elif self.board_off_since is not None and now - self.board_off_since >= BOARD_OFF_DEBOUNCE_SECONDS:
            self.board_off_since = None
            self.set_lidar(False)
        self.board_seen_on = board_on

    def set_lidar(self, on: bool) -> None:
        if self.lidar_active == on:
            return
        log.info("motor board %s: turning lidar %s", "answering" if on else "gone", "on" if on else "off")
        request = ChangeState.Request()
        request.transition.id = Transition.TRANSITION_ACTIVATE if on else Transition.TRANSITION_DEACTIVATE
        self.change_state_client.call_async(request).add_done_callback(self.on_lidar_changed)

    def on_lidar_changed(self, future) -> None:
        if not future.result().success:
            log.warning("lidar lifecycle transition refused")

    def publish_status(self) -> None:
        self.follow_board()
        # The answer lands in on_driver_state, in time for the next update.
        if self.state_client.service_is_ready():
            self.state_client.call_async(GetState.Request()).add_done_callback(self.on_driver_state)
        else:
            self.driver_state = None
            self.lidar_active = False
        now = time.monotonic()
        scan_hz = None
        if len(self.scan_times) >= 2:
            scan_hz = (len(self.scan_times) - 1) / (self.scan_times[-1] - self.scan_times[0])
        message = lidar_update(
            device_present=os.path.exists(LIDAR_DEVICE),
            service_active=service_active("robot-lidar.service"),
            lidar_active=self.lidar_active,
            nav_active=service_active("robot-nav.service"),
            scan_hz=scan_hz,
            scan_age_seconds=now - self.scan_times[-1] if self.scan_times else None,
            points=self.points,
            valid_points=self.valid_points,
            diagnostics=self.diagnostics,
            diagnostics_age_seconds=now - self.diagnostics_time if self.diagnostics_time is not None else None,
        )
        publish_message(self.telemetry_socket, message)


def service_active(unit: str) -> bool:
    try:
        result = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("systemctl is-active %s failed: %s", unit, exc)
        return False
    return result.stdout.strip() == "active"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot lidar status ROS node.")
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET)
    parser.add_argument("--telemetry-subscribe-socket", default=DEFAULT_SUBSCRIBE_SOCKET)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    rclpy.init()
    node = LidarStatusNode(args.telemetry_socket, args.telemetry_subscribe_socket)
    log.info("lidar status node starting")
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
    log.info("lidar status node stopped")


if __name__ == "__main__":
    main()
