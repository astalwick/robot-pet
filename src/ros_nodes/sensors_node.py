#!/usr/bin/env python3
"""ROS sensors node: the SensorsService loop, dual-publishing to the hub and ROS.

Owns a SensorsService exactly as robot_sensors.main() does. Every hub message
still goes to the telemetry socket (dashboards, safety gate), and the readings
inside it also go out as sensor_msgs/Range on /range/<name> and sensor_msgs/Imu
on /imu.
"""

import argparse
import threading
from typing import Any

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import Imu, Range

from config.sensors import DEFAULT_CONFIG_PATH
from lib.log import setup_logging
from robot_sensors import SensorsService, default_driver_factory, run_loop
from ros_nodes.ros_messages import imu_fields, range_fields
from telemetry.paths import DEFAULT_PUBLISH_SOCKET
from telemetry.socket_client import publish_message

log = setup_logging("robot-sensors")


class SensorsNode(Node):
    def __init__(self, telemetry_socket: str) -> None:
        super().__init__("robot_sensors")
        self.telemetry_socket = telemetry_socket
        self.range_publishers: dict[str, Any] = {}
        self.imu_publisher = self.create_publisher(Imu, "/imu", 10)

    def publish(self, message: dict[str, Any]) -> None:
        publish_message(self.telemetry_socket, message)
        stamp = self.get_clock().now().to_msg()
        for reading in message["readings"]:
            fields = range_fields(reading)
            if reading["name"] not in self.range_publishers:
                self.range_publishers[reading["name"]] = self.create_publisher(
                    Range, f"/range/{reading['name']}", 10
                )
            range_msg = Range()
            range_msg.header.stamp = stamp
            range_msg.header.frame_id = fields["frame_id"]
            range_msg.radiation_type = fields["radiation_type"]
            range_msg.field_of_view = fields["field_of_view"]
            range_msg.min_range = fields["min_range"]
            range_msg.max_range = fields["max_range"]
            range_msg.range = fields["range"]
            self.range_publishers[reading["name"]].publish(range_msg)

        fields = imu_fields(message.get("imu") or {})
        if fields is None:
            return
        imu_msg = Imu()
        imu_msg.header.stamp = stamp
        imu_msg.header.frame_id = fields["frame_id"]
        imu_msg.orientation.x, imu_msg.orientation.y, imu_msg.orientation.z, imu_msg.orientation.w = fields["orientation"]
        imu_msg.orientation_covariance = fields["orientation_covariance"]
        imu_msg.angular_velocity_covariance = fields["angular_velocity_covariance"]
        imu_msg.linear_acceleration_covariance = fields["linear_acceleration_covariance"]
        self.imu_publisher.publish(imu_msg)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot range sensor ROS node.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    rclpy.init()
    node = SensorsNode(args.telemetry_socket)
    service = SensorsService(
        config_path=args.config,
        publish=node.publish,
        driver_factory=default_driver_factory,
    )

    # The sensor loop keeps its own timing in a thread; rclpy.spin just keeps
    # the node alive and returns on SIGINT/SIGTERM.
    stop = threading.Event()
    loop = threading.Thread(target=run_loop, args=(service, stop), daemon=True)
    loop.start()
    log.info("sensors node starting")
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        stop.set()
        loop.join()
        service.cleanup()
        node.destroy_node()
        rclpy.try_shutdown()
    log.info("sensors node stopped")


if __name__ == "__main__":
    main()
