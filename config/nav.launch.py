"""Launch mapping or localization, plus Nav2 navigation, for robot-nav.service.

Reads ~/.config/robot-pet/nav.json, written by the nav bridge:

    {"mode": "mapping" | "localize", "map": "<name>"}

mapping: slam_toolbox online async with config/slam_toolbox.yaml builds /map
and publishes TF map -> odom, the same thing robot-slam.service used to run.

localize: nav2 map_server serves ~/.config/robot-pet/maps/<name>.yaml and AMCL
localizes against it with config/nav2.yaml. Nav2 1.5's localization_launch.py
has no lifecycle manager, so we start the three nodes ourselves.

Both modes also run the Nav2 navigation servers (planner, controller,
behaviors, bt_navigator) from config/nav2.yaml, so the nav bridge can send
NavigateToPose goals. Their cmd_vel goes to /cmd_vel_nav for twist_mux.

No nav.json, or a map file that is gone: mapping. A fresh robot maps.
"""

import json
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node

NAV_CONFIG_PATH = "/home/pi/.config/robot-pet/nav.json"
MAPS_DIR = "/home/pi/.config/robot-pet/maps"
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
NAV2_PARAMS = os.path.join(CONFIG_DIR, "nav2.yaml")
NAV_CMD_VEL_REMAP = [("cmd_vel", "cmd_vel_nav")]


def map_to_localize() -> str | None:
    """The map yaml to localize against, or None for mapping mode."""
    try:
        with open(NAV_CONFIG_PATH) as handle:
            nav = json.load(handle)
    except (OSError, ValueError):
        return None
    if nav.get("mode") != "localize" or not nav.get("map"):
        return None
    map_yaml = os.path.join(MAPS_DIR, f"{nav['map']}.yaml")
    return map_yaml if os.path.exists(map_yaml) else None


def mapping_nodes() -> list:
    slam_launch = os.path.join(get_package_share_directory("slam_toolbox"), "launch", "online_async_launch.py")
    return [
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(slam_launch),
            launch_arguments={
                "slam_params_file": os.path.join(CONFIG_DIR, "slam_toolbox.yaml"),
                "use_sim_time": "false",
            }.items(),
        )
    ]


def localize_nodes(map_yaml: str) -> list:
    return [
        Node(
            package="nav2_map_server",
            executable="map_server",
            name="map_server",
            output="screen",
            parameters=[NAV2_PARAMS, {"yaml_filename": map_yaml}],
        ),
        Node(
            package="nav2_amcl",
            executable="amcl",
            name="amcl",
            output="screen",
            parameters=[NAV2_PARAMS],
        ),
        Node(
            package="nav2_lifecycle_manager",
            executable="lifecycle_manager",
            name="lifecycle_manager_localization",
            output="screen",
            parameters=[{"autostart": True, "node_names": ["map_server", "amcl"]}],
        ),
    ]


def navigation_nodes() -> list:
    return [
        Node(
            package="nav2_controller",
            executable="controller_server",
            name="controller_server",
            output="screen",
            parameters=[NAV2_PARAMS],
            remappings=NAV_CMD_VEL_REMAP,
        ),
        Node(
            package="nav2_planner",
            executable="planner_server",
            name="planner_server",
            output="screen",
            parameters=[NAV2_PARAMS],
        ),
        Node(
            package="nav2_behaviors",
            executable="behavior_server",
            name="behavior_server",
            output="screen",
            parameters=[NAV2_PARAMS],
            remappings=NAV_CMD_VEL_REMAP,
        ),
        Node(
            package="nav2_bt_navigator",
            executable="bt_navigator",
            name="bt_navigator",
            output="screen",
            parameters=[NAV2_PARAMS],
        ),
        Node(
            package="nav2_lifecycle_manager",
            executable="lifecycle_manager",
            name="lifecycle_manager_navigation",
            output="screen",
            parameters=[
                {
                    "autostart": True,
                    "node_names": ["controller_server", "planner_server", "behavior_server", "bt_navigator"],
                }
            ],
        ),
    ]


def generate_launch_description() -> LaunchDescription:
    map_yaml = map_to_localize()
    mode_nodes = mapping_nodes() if map_yaml is None else localize_nodes(map_yaml)
    return LaunchDescription(mode_nodes + navigation_nodes())
