#!/usr/bin/env python3
"""ROS lidar status node: watches the apt rplidar driver for the dashboard, and
turns the lidar on and off with the motor rail.

The driver (robot-lidar.service) only speaks ROS, so this node listens to its
/scan and /diagnostics, asks for its lifecycle state (active means the lidar is
on), and twice a second sends a lidar_update to the hub, alongside whether the
lidar and SLAM services are running.

It also reads the hub's motor_rail state: when the rail comes on the lidar is
activated, and a few seconds after the rail drops it is deactivated. Only the
edges matter, so the dashboard's manual on/off holds until the rail next
changes.
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
# bouncing rail from spinning the lidar up and down with it.
RAIL_OFF_DEBOUNCE_SECONDS = 3.0


def rail_is_on(snapshot: dict) -> bool:
    source = (snapshot.get("sources") or {}).get("motor_rail") or {}
    if source.get("stale") is not False:
        return False
    motor_rail = snapshot.get("motor_rail") or {}
    return motor_rail.get("state") in ("on", "warning")


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
        # written by the hub thread, read by the timer
        self.rail_on = False
        # the rail state the timer last acted on; None until the first tick
        self.rail_seen_on: bool | None = None
        self.rail_off_since: float | None = None
        self.driver_ready = False
        self.state_client = self.create_client(GetState, "/rplidar_node/get_state")
        self.change_state_client = self.create_client(ChangeState, "/rplidar_node/change_state")
        self.create_subscription(LaserScan, "/scan", self.on_scan, qos_profile_sensor_data)
        self.create_subscription(DiagnosticArray, "/diagnostics", self.on_diagnostics, 10)
        self.create_timer(0.5, self.publish_status)
        threading.Thread(target=self.watch_rail, daemon=True).start()

    def watch_rail(self) -> None:
        for snapshot in subscribe(self.subscribe_socket):
            self.rail_on = rail_is_on(snapshot)

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
        self.lidar_active = future.result().current_state.label == "active"

    def follow_rail(self) -> None:
        if not self.state_client.service_is_ready():
            self.driver_ready = False
            return
        # A driver that just (re)started comes up inactive, so apply the rail once.
        driver_appeared = not self.driver_ready
        self.driver_ready = True
        rail_on = self.rail_on
        now = time.monotonic()
        if rail_on:
            self.rail_off_since = None
            if driver_appeared or self.rail_seen_on is not True:
                self.set_lidar(True)
        elif self.rail_seen_on is True:
            self.rail_off_since = now
        elif self.rail_off_since is not None and now - self.rail_off_since >= RAIL_OFF_DEBOUNCE_SECONDS:
            self.rail_off_since = None
            self.set_lidar(False)
        self.rail_seen_on = rail_on

    def set_lidar(self, on: bool) -> None:
        if self.lidar_active == on:
            return
        log.info("motor rail %s: turning lidar %s", "on" if on else "off", "on" if on else "off")
        request = ChangeState.Request()
        request.transition.id = Transition.TRANSITION_ACTIVATE if on else Transition.TRANSITION_DEACTIVATE
        self.change_state_client.call_async(request).add_done_callback(self.on_lidar_changed)

    def on_lidar_changed(self, future) -> None:
        if not future.result().success:
            log.warning("lidar lifecycle transition refused")

    def publish_status(self) -> None:
        self.follow_rail()
        # The answer lands in on_driver_state, in time for the next update.
        if self.state_client.service_is_ready():
            self.state_client.call_async(GetState.Request()).add_done_callback(self.on_driver_state)
        else:
            self.lidar_active = False
        now = time.monotonic()
        scan_hz = None
        if len(self.scan_times) >= 2:
            scan_hz = (len(self.scan_times) - 1) / (self.scan_times[-1] - self.scan_times[0])
        message = lidar_update(
            device_present=os.path.exists(LIDAR_DEVICE),
            service_active=service_active("robot-lidar.service"),
            lidar_active=self.lidar_active,
            slam_active=service_active("robot-slam.service"),
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
