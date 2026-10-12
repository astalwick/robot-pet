#!/bin/bash
# Run a robot-pet ROS node: ROS env + repo venv python.
# Usage from a systemd unit: ExecStart=/home/pi/robot-pet/scripts/ros-node.sh /home/pi/robot-pet/src/ros_nodes/foo_node.py
set -euo pipefail
source /opt/ros/lyrical/setup.bash
set -a; source /home/pi/.config/robot-pet/ros.env; set +a
exec /home/pi/robot-pet/.venv/bin/python "$@"
