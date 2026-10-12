#!/usr/bin/env python3
"""ROS teleop node: GamepadTeleopRunner publishing /cmd_vel_teleop.

The runner keeps owning the controller loop in its own thread; this node only
turns its body twists into geometry_msgs/Twist. twist_mux merges the topic into
/cmd_vel for robot-motion.
"""

import threading

import rclpy
from geometry_msgs.msg import Twist
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node

from control.commands import MotionCommand
from gamepad_teleop import GamepadTeleopRunner, build_config, build_parser
from lib.log import setup_logging

log = setup_logging("gamepad-teleop")


class GamepadTeleopNode(Node):
    def __init__(self) -> None:
        super().__init__("gamepad_teleop")
        self.twist_publisher = self.create_publisher(Twist, "/cmd_vel_teleop", 10)

    def publish_twist(self, command: MotionCommand) -> None:
        twist = Twist()
        twist.linear.x = command.linear_x
        twist.angular.z = command.angular_z
        self.twist_publisher.publish(twist)


def main() -> None:
    args = build_parser().parse_args()
    rclpy.init()
    node = GamepadTeleopNode()
    runner = GamepadTeleopRunner(build_config(args), twist_publisher=node.publish_twist)

    teleop_thread = threading.Thread(target=runner.run_forever, daemon=True)
    teleop_thread.start()
    log.info("teleop node starting")
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        runner.request_stop()
        teleop_thread.join()
        node.destroy_node()
        rclpy.try_shutdown()
    log.info("teleop node stopped")


if __name__ == "__main__":
    main()
