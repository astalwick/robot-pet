"""Scaffolding node: proves the systemd + ros-node.sh + rclpy pattern.

Publishes a counter on /robot/heartbeat at 1 Hz. Deleted in migration Phase 7
once real nodes exist.
"""

import rclpy
from rclpy.node import Node
from std_msgs.msg import String


class HeartbeatNode(Node):
    def __init__(self) -> None:
        super().__init__("robot_heartbeat")
        self.publisher = self.create_publisher(String, "/robot/heartbeat", 10)
        self.count = 0
        self.create_timer(1.0, self.tick)

    def tick(self) -> None:
        self.count += 1
        self.publisher.publish(String(data=f"heartbeat {self.count}"))


def main() -> None:
    rclpy.init()
    node = HeartbeatNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
