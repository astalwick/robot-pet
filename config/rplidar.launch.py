"""Launch the RPLidar A1 driver for robot-lidar.service.

rplidar_node is a lifecycle node: "active" scans with the motor spinning,
"inactive" is idle. We configure and activate it once at startup. After that
anything can turn the lidar off and on with `deactivate` / `activate` on
/rplidar_node/change_state.

The package's own rplidar_with_args.launch.py re-activates the node every time
it reaches "inactive", so a deactivate is undone within a fraction of a second.
Here we only activate when it gets to "inactive" by being configured.
"""

from launch import LaunchDescription
from launch.actions import EmitEvent, RegisterEventHandler
from launch.event_handlers import OnProcessStart
from launch.events import matches_action
from launch_ros.actions import LifecycleNode
from launch_ros.event_handlers import OnStateTransition
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
    activate_once_configured = RegisterEventHandler(
        OnStateTransition(
            target_lifecycle_node=driver_node,
            start_state="configuring",
            goal_state="inactive",
            entities=[
                EmitEvent(
                    event=ChangeState(
                        lifecycle_node_matcher=matches_action(driver_node),
                        transition_id=Transition.TRANSITION_ACTIVATE,
                    )
                )
            ],
        )
    )
    return LaunchDescription([driver_node, configure_on_start, activate_once_configured])
