"""Launch mapping or localization for robot-nav.service.

Reads ~/.config/robot-pet/nav.json, written by the nav bridge:

    {"mode": "mapping" | "localize", "map": "<name>"}

mapping: slam_toolbox online async with config/slam_toolbox.yaml builds /map
and publishes TF map -> odom, the same thing robot-slam.service used to run.

localize: nav2 map_server serves ~/.config/robot-pet/maps/<name>.yaml and AMCL
localizes against it with config/nav2.yaml. Nav2 1.5's localization_launch.py
has no lifecycle manager, so we start the three nodes ourselves.

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


def generate_launch_description() -> LaunchDescription:
    map_yaml = map_to_localize()
    if map_yaml is None:
        slam_launch = os.path.join(get_package_share_directory("slam_toolbox"), "launch", "online_async_launch.py")
        return LaunchDescription(
            [
                IncludeLaunchDescription(
                    PythonLaunchDescriptionSource(slam_launch),
                    launch_arguments={
                        "slam_params_file": os.path.join(CONFIG_DIR, "slam_toolbox.yaml"),
                        "use_sim_time": "false",
                    }.items(),
                )
            ]
        )

    nav2_params = os.path.join(CONFIG_DIR, "nav2.yaml")
    return LaunchDescription(
        [
            Node(
                package="nav2_map_server",
                executable="map_server",
                name="map_server",
                output="screen",
                parameters=[nav2_params, {"yaml_filename": map_yaml}],
            ),
            Node(
                package="nav2_amcl",
                executable="amcl",
                name="amcl",
                output="screen",
                parameters=[nav2_params],
            ),
            Node(
                package="nav2_lifecycle_manager",
                executable="lifecycle_manager",
                name="lifecycle_manager_localization",
                output="screen",
                parameters=[{"autostart": True, "node_names": ["map_server", "amcl"]}],
            ),
        ]
    )
