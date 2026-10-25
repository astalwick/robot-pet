"""Launch the RPLidar A1 driver for robot-lidar.service.

rplidar_node is a lifecycle node: "active" scans with the motor spinning,
"inactive" is idle. We only configure it here, so the lidar starts off.
robot-lidar-status activates it while the motor rail is on and deactivates it
when the rail drops; the dashboard button does the same by hand.

The package's own rplidar_with_args.launch.py re-activates the node every time
it reaches "inactive", so a deactivate is undone within a fraction of a second.
"""

from launch import LaunchDescription
from launch.actions import EmitEvent, RegisterEventHandler
from launch.event_handlers import OnProcessStart
from launch.events import matches_action
from launch_ros.actions import LifecycleNode
from launch_ros.events.lifecycle import ChangeState
from lifecycle_msgs.msg import Transition


def generate_launch_description() -> LaunchDescription:
    driver_node = LifecycleNode(
        package="rplidar_driver",
        executable="rplidar_node",
        name="rplidar_node",
        namespace="",
        output="screen",
        parameters=[
            {
                "serial_port": "/dev/rplidar",
                "serial_baudrate": 115200,
                "frame_id": "laser",
                "angle_compensate": True,
                # rplidar_with_args.launch.py set these two; keep the scans identical
                "intensities_as_angles": True,
                "interpolated_rays": True,
                # the URDF owns the laser frame
                "publish_tf": False,
            }
        ],
    )
    configure_on_start = RegisterEventHandler(
        OnProcessStart(
            target_action=driver_node,
            on_start=[
                EmitEvent(
                    event=ChangeState(
                        lifecycle_node_matcher=matches_action(driver_node),
                        transition_id=Transition.TRANSITION_CONFIGURE,
                    )
                )
            ],
        )
    )
    return LaunchDescription([driver_node, configure_on_start])
