#!/usr/bin/env python3
"""ROS Pi-battery node: PiBatteryService with the hub publish plus /battery/pi."""

import threading

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import BatteryState

from lib.log import setup_logging
from robot_pi_battery import PiBatteryConfig, PiBatteryService, build_parser, run_loop
from ros_nodes.ros_messages import pi_battery_state_fields
from ros_nodes.service_thread import start_service_thread
from telemetry.socket_client import publish_message

log = setup_logging("robot-pi-battery")


class PiBatteryNode(Node):
    def __init__(self, telemetry_socket: str) -> None:
        super().__init__("robot_pi_battery")
        self.telemetry_socket = telemetry_socket
        self.battery_publisher = self.create_publisher(BatteryState, "/battery/pi", 10)

    def publish(self, message: dict) -> None:
        publish_message(self.telemetry_socket, message)
        fields = pi_battery_state_fields(message)
        if fields is None:
            return
        battery = BatteryState()
        battery.header.stamp = self.get_clock().now().to_msg()
        battery.header.frame_id = fields["frame_id"]
        battery.voltage = fields["voltage"]
        battery.current = fields["current"]
        battery.charge = fields["charge"]
        battery.percentage = fields["percentage"]
        battery.power_supply_status = fields["power_supply_status"]
        battery.power_supply_health = fields["power_supply_health"]
        battery.power_supply_technology = fields["power_supply_technology"]
        battery.present = fields["present"]
        battery.cell_voltage = fields["cell_voltage"]
        self.battery_publisher.publish(battery)


def main() -> None:
    args = build_parser().parse_args()
    config = PiBatteryConfig(
        bus=args.bus,
        address=args.address,
        poll_interval=args.poll_interval,
        warning_voltage=args.warning_voltage,
        shutdown_voltage=args.shutdown_voltage,
        telemetry_socket=args.telemetry_socket,
    )
    rclpy.init()
    node = PiBatteryNode(config.telemetry_socket)
    service = PiBatteryService(config, publish=node.publish)

    stop = threading.Event()
    service_thread = start_service_thread(run_loop, service, stop)
    log.info("pi battery node starting")
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        stop.set()
        service_thread.join()
        service.cleanup()
        node.destroy_node()
        rclpy.try_shutdown()
    log.info("pi battery node stopped")


if __name__ == "__main__":
    main()
