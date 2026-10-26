#!/usr/bin/env python3
"""ROS motion node: MotionRunner with /cmd_vel and /scan in and /odom, TF, battery out.

The motor loop keeps owning timing in its own thread, exactly as robot_motion
main() runs it. This node feeds it twists from /cmd_vel and each /scan reduced
to forward and rear clearance for the safety gate, republishes its hub
telemetry as ROS messages, and broadcasts odom -> base_link at a fixed rate so
slam_toolbox always has a fresh transform.
"""

from typing import Any

import rclpy
from geometry_msgs.msg import TransformStamped, Twist
from nav_msgs.msg import Odometry
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import BatteryState, LaserScan
from std_msgs.msg import Bool, Float32MultiArray
from tf2_ros import TransformBroadcaster

from control.commands import MotionCommand
from control.safety_gate import Clearances, scan_clearances
from lib.log import setup_logging
from robot_motion import MotionRunner, build_parser, build_runner
from ros_nodes.ros_messages import battery_state_fields, odometry_fields
from ros_nodes.service_thread import start_service_thread
from telemetry.socket_client import publish_message

# Not tied to the hub telemetry cadence: slam_toolbox interpolates the robot
# pose from this TF for every lidar scan (~10 Hz), so it must be faster.
ODOM_PUBLISH_HZ = 20

log = setup_logging("robot-motion")


class MotionNode(Node):
    def __init__(self) -> None:
        super().__init__("robot_motion")
        self.runner: MotionRunner | None = None
        self.create_subscription(Twist, "/cmd_vel", self.on_cmd_vel, 10)
        self.create_subscription(LaserScan, "/scan", self.on_scan, qos_profile_sensor_data)
        # [forward_m, rear_m] to the nearest barrier, from the nav bridge while it knows the map pose
        self.create_subscription(Float32MultiArray, "/barrier_clearance", self.on_barrier_clearance, 10)
        self.odom_publisher = self.create_publisher(Odometry, "/odom", 10)
        self.battery_publisher = self.create_publisher(BatteryState, "/battery/motor", 10)
        # twist_mux lock: true while an intent drives, so Nav2 cannot talk over it
        self.nav_lock_publisher = self.create_publisher(Bool, "/nav_lock", 10)
        self.tf_broadcaster = TransformBroadcaster(self)
        self.create_timer(1.0 / ODOM_PUBLISH_HZ, self.publish_odometry)

    def on_cmd_vel(self, twist: Twist) -> None:
        self.runner.on_twist_command(MotionCommand(linear_x=twist.linear.x, angular_z=twist.angular.z))

    def on_scan(self, scan: LaserScan) -> None:
        self.runner.on_scan_clearances(
            scan_clearances(scan.ranges, scan.angle_min, scan.angle_increment, scan.range_min, scan.range_max)
        )

    def on_barrier_clearance(self, message: Float32MultiArray) -> None:
        forward_m, rear_m = message.data
        self.runner.on_barrier_clearances(
            Clearances(None if forward_m == float("inf") else forward_m, None if rear_m == float("inf") else rear_m)
        )

    def publish_odometry(self) -> None:
        pose, twist = self.runner.odometry_state()
        fields = odometry_fields(pose.x, pose.y, pose.theta, twist.linear_x, twist.angular_z)
        stamp = self.get_clock().now().to_msg()

        odom = Odometry()
        odom.header.stamp = stamp
        odom.header.frame_id = fields["frame_id"]
        odom.child_frame_id = fields["child_frame_id"]
        odom.pose.pose.position.x, odom.pose.pose.position.y, odom.pose.pose.position.z = fields["position"]
        orientation = odom.pose.pose.orientation
        orientation.x, orientation.y, orientation.z, orientation.w = fields["orientation"]
        odom.pose.covariance = fields["pose_covariance"]
        odom.twist.twist.linear.x, odom.twist.twist.linear.y, odom.twist.twist.linear.z = fields["linear"]
        odom.twist.twist.angular.x, odom.twist.twist.angular.y, odom.twist.twist.angular.z = fields["angular"]
        odom.twist.covariance = fields["twist_covariance"]
        self.odom_publisher.publish(odom)

        transform = TransformStamped()
        transform.header.stamp = stamp
        transform.header.frame_id = fields["frame_id"]
        transform.child_frame_id = fields["child_frame_id"]
        transform.transform.translation.x, transform.transform.translation.y, transform.transform.translation.z = fields["position"]
        rotation = transform.transform.rotation
        rotation.x, rotation.y, rotation.z, rotation.w = fields["orientation"]
        self.tf_broadcaster.sendTransform(transform)
        self.nav_lock_publisher.publish(Bool(data=self.runner.nav_lock_active()))

    def publish_hub(self, socket_path: str, message: dict[str, Any]) -> bool:
        """MotionRunner's telemetry_publisher: hub first, then the ROS copies."""
        published = publish_message(socket_path, message)
        fields = battery_state_fields(message.get("motor_battery") or {})
        if fields is not None:
            battery = BatteryState()
            battery.header.stamp = self.get_clock().now().to_msg()
            battery.header.frame_id = fields["frame_id"]
            battery.voltage = fields["voltage"]
            battery.percentage = fields["percentage"]
            battery.power_supply_status = fields["power_supply_status"]
            battery.power_supply_health = fields["power_supply_health"]
            battery.power_supply_technology = fields["power_supply_technology"]
            battery.present = fields["present"]
            self.battery_publisher.publish(battery)
        return published


def main() -> None:
    args = build_parser().parse_args()
    rclpy.init()
    node = MotionNode()
    node.runner = build_runner(args, telemetry_publisher=node.publish_hub)

    motor_thread = start_service_thread(node.runner.run_forever)
    log.info("motion node starting")
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.runner.request_stop()
        motor_thread.join()
        node.destroy_node()
        rclpy.try_shutdown()
    log.info("motion node stopped")


if __name__ == "__main__":
    main()
