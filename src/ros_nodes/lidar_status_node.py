#!/usr/bin/env python3
"""ROS lidar status node: watches the apt rplidar driver for the dashboard.

The driver (robot-lidar.service) only speaks ROS, so this node listens to its
/scan and /diagnostics and twice a second sends a lidar_update to the hub,
alongside whether the lidar and SLAM services are running.
"""

import argparse
import os
import subprocess
import time
from collections import deque

import rclpy
from diagnostic_msgs.msg import DiagnosticArray
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import LaserScan

from lib.log import setup_logging
from telemetry.messages import lidar_update
from telemetry.paths import DEFAULT_PUBLISH_SOCKET
from telemetry.socket_client import publish_message

log = setup_logging("robot-lidar-status")

# The udev rule symlinks the A1 here when it is plugged in.
LIDAR_DEVICE = "/dev/rplidar"


class LidarStatusNode(Node):
    def __init__(self, telemetry_socket: str) -> None:
        super().__init__("robot_lidar_status")
        self.telemetry_socket = telemetry_socket
        # receive times of the last few scans, for the scan rate
        self.scan_times: deque[float] = deque(maxlen=20)
        self.points: int | None = None
        self.valid_points: int | None = None
        self.diagnostics: list[dict] = []
        self.diagnostics_time: float | None = None
        self.create_subscription(LaserScan, "/scan", self.on_scan, qos_profile_sensor_data)
        self.create_subscription(DiagnosticArray, "/diagnostics", self.on_diagnostics, 10)
        self.create_timer(0.5, self.publish_status)

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

    def publish_status(self) -> None:
        now = time.monotonic()
        scan_hz = None
        if len(self.scan_times) >= 2:
            scan_hz = (len(self.scan_times) - 1) / (self.scan_times[-1] - self.scan_times[0])
        message = lidar_update(
            device_present=os.path.exists(LIDAR_DEVICE),
            service_active=service_active("robot-lidar.service"),
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
    return parser


def main() -> None:
    args = build_parser().parse_args()
    rclpy.init()
    node = LidarStatusNode(args.telemetry_socket)
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
