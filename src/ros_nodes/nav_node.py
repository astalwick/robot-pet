#!/usr/bin/env python3
"""ROS nav bridge node: maps, places, relocalization and the `nav` hub source.

robot-nav.service runs slam_toolbox (mapping) or map_server + AMCL (localize),
picked by nav.json. This node sits next to it and is the only thing that writes
nav.json: the dashboard and the voice tools talk to it over
/run/robot-pet/nav.sock, and it restarts robot-nav to switch modes.

Twice a second it publishes a nav_update to the hub: mode, map, pose (TF
map -> base_link), the saved maps and places, and whether it wants the motor
rail. It writes /map as /run/robot-pet/map.png for the dashboard.

Localize mode has to tell AMCL where the robot is. Whenever AMCL becomes
active (boot, mode switch, a redeploy of robot-nav) the bridge either sends
the pose it remembered in /run/robot-pet/nav-pose.json (same map, robot has
not been moved since) or relocalizes: asks for the motor rail, waits for
scans, and for each saved map loads it, scatters the particles and spins the
robot once with a `turn` intent. The first map whose AMCL covariance gets
tight wins. Saving a map goes straight to localize mode on that map with
slam's last pose, so no spin is needed.
"""

import argparse
import json
import math
import subprocess
import threading
import time
from pathlib import Path

import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped
from lifecycle_msgs.srv import GetState
from nav2_msgs.srv import LoadMap
from nav_msgs.msg import OccupancyGrid
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import LaserScan
from std_srvs.srv import Empty
from tf2_ros import Buffer, TransformException, TransformListener

import robot_nav
from control.motion_intent import request_motion_intent
from lib.log import setup_logging
from telemetry.messages import nav_update
from telemetry.paths import (
    DEFAULT_MAP_PNG_PATH,
    DEFAULT_MOTION_INTENT_SOCKET,
    DEFAULT_NAV_POSE_PATH,
    DEFAULT_NAV_SOCKET,
    DEFAULT_PUBLISH_SOCKET,
)
from telemetry.socket_client import publish_message

log = setup_logging("robot-nav-bridge")

# slam_toolbox and map_server both latch /map (transient local, reliable).
MAP_QOS = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL, reliability=ReliabilityPolicy.RELIABLE)
MAP_PNG_MIN_INTERVAL = 2.0
# After asking for the rail: battery turns it on, the lidar activates and spins up.
SCAN_WAIT_SECONDS = 20.0
SERVICE_WAIT_SECONDS = 10.0
# The covariance AMCL starts from when we hand it a remembered pose.
INITIAL_POSE_XY_VARIANCE = 0.25
INITIAL_POSE_YAW_VARIANCE = 0.07


class NavNode(Node):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("robot_nav_bridge")
        self.args = args
        self.nav = robot_nav.load_nav_config(args.nav_config)
        self.mode, self.map_name = robot_nav.resolve_mode(self.nav, args.maps_dir)
        self.places = robot_nav.load_places(args.places)
        self.localized = False
        self.activity: str | None = None
        self.motor_power_requested = False
        # best known pose in the map frame: TF when it resolves, else remembered
        self.pose: dict[str, float] | None = None
        self.remembered_pose_written: dict | None = None
        self.map_info: dict | None = None
        self.map_serial = 0
        self.latest_map: OccupancyGrid | None = None
        self.map_png_time = 0.0
        self.last_scan_time: float | None = None
        self.amcl_covariance: list[float] | None = None
        # the AMCL lifecycle state the timer last saw; None until the first tick
        self.amcl_active: bool | None = None
        # one localize or save at a time
        self.busy = threading.Lock()

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.create_subscription(OccupancyGrid, "/map", self.on_map, MAP_QOS)
        self.create_subscription(PoseWithCovarianceStamped, "/amcl_pose", self.on_amcl_pose, 10)
        self.create_subscription(LaserScan, "/scan", self.on_scan, qos_profile_sensor_data)
        self.initial_pose_publisher = self.create_publisher(PoseWithCovarianceStamped, "/initialpose", 10)
        self.amcl_state_client = self.create_client(GetState, "/amcl/get_state")
        self.load_map_client = self.create_client(LoadMap, "/map_server/load_map")
        self.global_localization_client = self.create_client(Empty, "/reinitialize_global_localization")
        self.create_timer(0.5, self.tick)
        self.server = robot_nav.NavSocketServer(args.nav_socket, self.handle_request)
        self.server.start()

    # --- ROS callbacks ---------------------------------------------------

    def on_map(self, grid: OccupancyGrid) -> None:
        self.latest_map = grid

    def on_amcl_pose(self, message: PoseWithCovarianceStamped) -> None:
        self.amcl_covariance = list(message.pose.covariance)

    def on_scan(self, scan: LaserScan) -> None:
        self.last_scan_time = time.monotonic()

    def on_amcl_state(self, future) -> None:
        active = future.result().current_state.label == "active"
        if active and self.amcl_active is not True:
            self.on_amcl_became_active()
        self.amcl_active = active

    # --- the 0.5 s tick ----------------------------------------------------

    def tick(self) -> None:
        if self.amcl_state_client.service_is_ready():
            self.amcl_state_client.call_async(GetState.Request()).add_done_callback(self.on_amcl_state)
        else:
            self.amcl_active = False
        self.update_pose()
        self.update_map_png()
        self.remember_pose()
        publish_message(self.args.telemetry_socket, nav_update(**self.status()))

    def update_pose(self) -> None:
        try:
            transform = self.tf_buffer.lookup_transform("map", "base_link", Time()).transform
        except TransformException:
            return
        rotation = transform.rotation
        self.pose = {
            "x": transform.translation.x,
            "y": transform.translation.y,
            "yaw": robot_nav.yaw_from_quaternion(rotation.x, rotation.y, rotation.z, rotation.w),
        }

    def update_map_png(self) -> None:
        grid = self.latest_map
        if grid is None or time.monotonic() - self.map_png_time < MAP_PNG_MIN_INTERVAL:
            return
        self.latest_map = None
        self.map_png_time = time.monotonic()
        info = grid.info
        try:
            robot_nav.render_map_png(info.width, info.height, grid.data, self.args.map_png)
        except OSError as exc:
            log.warning("could not write %s: %s", self.args.map_png, exc)
            return
        self.map_serial += 1
        self.map_info = {
            "width": info.width,
            "height": info.height,
            "resolution": info.resolution,
            "origin_x": info.origin.position.x,
            "origin_y": info.origin.position.y,
            "serial": self.map_serial,
        }

    def remember_pose(self) -> None:
        """Keep the localized pose in /run so a restart of this node or of
        AMCL does not cost a spin. /run is cleared on reboot on purpose: a
        powered-off robot may have been carried somewhere else."""
        if not self.localized or self.mode != "localize" or self.pose is None:
            return
        remembered = {"map": self.map_name, **self.pose}
        if remembered == self.remembered_pose_written:
            return
        try:
            Path(self.args.nav_pose).write_text(json.dumps(remembered))
        except OSError as exc:
            log.warning("could not write %s: %s", self.args.nav_pose, exc)
            return
        self.remembered_pose_written = remembered

    def remembered_pose(self) -> dict | None:
        try:
            remembered = json.loads(Path(self.args.nav_pose).read_text())
        except (OSError, ValueError):
            return None
        return remembered if remembered.get("map") == self.map_name else None

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "map_name": self.map_name,
            "maps": robot_nav.saved_maps(self.args.maps_dir),
            "localized": self.localized,
            "activity": self.activity,
            "pose": self.pose,
            "map_info": self.map_info,
            "places": self.places,
            "motor_power_requested": self.motor_power_requested,
        }

    # --- localize mode -----------------------------------------------------

    def on_amcl_became_active(self) -> None:
        remembered = self.remembered_pose()
        if remembered is not None:
            log.info("AMCL active: sending the remembered pose on %s", self.map_name)
            self.set_initial_pose(remembered)
            return
        log.info("AMCL active with no remembered pose: relocalizing")
        maps = robot_nav.maps_to_try(robot_nav.saved_maps(self.args.maps_dir), self.map_name)
        self.start_activity("localizing", self.relocalize, maps)

    def set_initial_pose(self, pose: dict) -> None:
        message = PoseWithCovarianceStamped()
        message.header.frame_id = "map"
        message.header.stamp = self.get_clock().now().to_msg()
        message.pose.pose.position.x = float(pose["x"])
        message.pose.pose.position.y = float(pose["y"])
        message.pose.pose.orientation.z = math.sin(pose["yaw"] / 2)
        message.pose.pose.orientation.w = math.cos(pose["yaw"] / 2)
        message.pose.covariance[0] = INITIAL_POSE_XY_VARIANCE
        message.pose.covariance[7] = INITIAL_POSE_XY_VARIANCE
        message.pose.covariance[35] = INITIAL_POSE_YAW_VARIANCE
        self.initial_pose_publisher.publish(message)
        self.pose = {"x": pose["x"], "y": pose["y"], "yaw": pose["yaw"]}
        self.localized = True

    def start_activity(self, name: str, target, *args) -> dict:
        """Run target on its own thread unless a localize or save is already
        running. Returns the socket ack: accepted, or not because busy."""
        if not self.busy.acquire(blocking=False):
            return {"ok": True, "accepted": False, "reason": "busy"}
        self.activity = name

        def run() -> None:
            try:
                target(*args)
            finally:
                self.activity = None
                self.busy.release()

        threading.Thread(target=run, name=f"nav-{name}", daemon=True).start()
        return {"ok": True, "accepted": True}

    def relocalize(self, maps: list[str]) -> None:
        self.localized = False
        self.motor_power_requested = True
        try:
            if not self.wait_for_scans():
                log.warning("relocalize: no scans after %.0fs, giving up", SCAN_WAIT_SECONDS)
                return
            for name in maps:
                if self.try_map(name):
                    log.info("relocalize: converged on %s", name)
                    self.map_name = name
                    robot_nav.save_nav_config("localize", name, self.args.nav_config)
                    self.localized = True
                    return
            log.warning("relocalize: no saved map converged (%s)", ", ".join(maps) or "no maps")
        finally:
            self.motor_power_requested = False

    def wait_for_scans(self) -> bool:
        deadline = time.monotonic() + SCAN_WAIT_SECONDS
        while time.monotonic() < deadline:
            if self.last_scan_time is not None and time.monotonic() - self.last_scan_time < 1.0:
                return True
            time.sleep(0.2)
        return False

    def try_map(self, name: str) -> bool:
        log.info("relocalize: trying %s", name)
        if not self.load_map_client.wait_for_service(SERVICE_WAIT_SECONDS):
            log.warning("relocalize: map_server not available")
            return False
        request = LoadMap.Request()
        request.map_url = robot_nav.map_yaml_path(name, self.args.maps_dir)
        try:
            response = self.load_map_client.call(request, timeout_sec=SERVICE_WAIT_SECONDS)
            if response.result != LoadMap.Response.RESULT_SUCCESS:
                log.warning("relocalize: load_map %s failed with result %d", name, response.result)
                return False
            self.global_localization_client.call(Empty.Request(), timeout_sec=SERVICE_WAIT_SECONDS)
        except TimeoutError:
            log.warning("relocalize: AMCL or map_server did not answer")
            return False
        self.amcl_covariance = None
        result = request_motion_intent(self.args.motion_intent_socket, "turn", degrees=360)
        if not result.get("ok"):
            log.warning("relocalize: turn failed: %s", result.get("error"))
        # the last scan after the spin stops still updates the filter
        time.sleep(1.0)
        return robot_nav.is_localized(self.amcl_covariance)

    # --- the nav socket ----------------------------------------------------

    def handle_request(self, request: dict) -> dict:
        cmd = request.get("cmd")
        if cmd == "nav_status":
            return {"ok": True, **self.status()}
        if cmd == "set_mode":
            return self.set_mode(request.get("mode"), request.get("map"))
        if cmd == "save_map":
            name = request.get("name")
            if not robot_nav.valid_map_name(name):
                return {"ok": False, "error": "bad_map_name"}
            if self.mode != "mapping":
                return {"ok": False, "error": "not_mapping"}
            return self.start_activity("saving", self.save_map, name)
        if cmd == "localize":
            if self.mode != "localize":
                return {"ok": False, "error": "not_localize_mode"}
            maps = robot_nav.saved_maps(self.args.maps_dir)
            if request.get("map"):
                if request["map"] not in maps:
                    return {"ok": False, "error": "unknown_map"}
                maps = [request["map"]]
            else:
                maps = robot_nav.maps_to_try(maps, self.map_name)
            return self.start_activity("localizing", self.relocalize, maps)
        if cmd == "create_place":
            return self.create_place(request.get("name"))
        return {"ok": False, "error": "unknown_cmd"}

    def set_mode(self, mode, map_name) -> dict:
        if mode == "mapping":
            map_name = None
        elif mode == "localize":
            if map_name not in robot_nav.saved_maps(self.args.maps_dir):
                return {"ok": False, "error": "unknown_map"}
        else:
            return {"ok": False, "error": "bad_mode"}
        self.switch_to(mode, map_name)
        return {"ok": True}

    def switch_to(self, mode: str, map_name: str | None) -> None:
        """Write nav.json and restart robot-nav so nav.launch.py picks it up."""
        log.info("switching to %s%s", mode, f" on {map_name}" if map_name else "")
        robot_nav.save_nav_config(mode, map_name, self.args.nav_config)
        self.mode, self.map_name = mode, map_name
        self.localized = False
        self.amcl_covariance = None
        # The old AMCL may still answer "active" for a moment. Count it as seen
        # so the edge we act on is the new one coming up.
        self.amcl_active = True
        # On its own thread: the socket reply must not wait for slam_toolbox to
        # stop (up to 10 s), and save_map's thread has no reason to either.
        threading.Thread(target=self.restart_robot_nav, name="nav-restart", daemon=True).start()

    def restart_robot_nav(self) -> None:
        try:
            subprocess.run(["sudo", "-n", "systemctl", "restart", "robot-nav.service"], check=False, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("restart robot-nav failed: %s", exc)
        self.amcl_active = False

    def save_map(self, name: str) -> None:
        """map_saver_cli writes maps/<name>.yaml + .pgm from the latched /map,
        then the robot goes to localize mode on that map at slam's pose."""
        pose = self.pose
        target = str(Path(self.args.maps_dir) / name)
        Path(self.args.maps_dir).mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                ["ros2", "run", "nav2_map_server", "map_saver_cli", "-f", target],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("save_map %s failed: %s", name, exc)
            return
        if result.returncode != 0 or not Path(f"{target}.yaml").exists():
            log.warning("save_map %s failed: %s", name, (result.stderr or result.stdout).strip()[-400:])
            return
        log.info("saved map %s", name)
        if pose is not None:
            Path(self.args.nav_pose).write_text(json.dumps({"map": name, **pose}))
        self.switch_to("localize", name)

    def create_place(self, name) -> dict:
        if not robot_nav.valid_place_name(name):
            return {"ok": False, "error": "bad_place_name"}
        if self.mode != "localize" or not self.localized or self.pose is None:
            return {"ok": False, "error": "not_localized"}
        self.places[name] = {"map": self.map_name, **self.pose}
        robot_nav.save_places(self.places, self.args.places)
        log.info("saved place %s on %s", name, self.map_name)
        return {"ok": True, "place": self.places[name]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot nav bridge ROS node.")
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET)
    parser.add_argument("--nav-socket", default=DEFAULT_NAV_SOCKET)
    parser.add_argument("--motion-intent-socket", default=DEFAULT_MOTION_INTENT_SOCKET)
    parser.add_argument("--nav-config", default=robot_nav.DEFAULT_NAV_CONFIG_PATH)
    parser.add_argument("--places", default=robot_nav.DEFAULT_PLACES_PATH)
    parser.add_argument("--maps-dir", default=robot_nav.DEFAULT_MAPS_DIR)
    parser.add_argument("--map-png", default=DEFAULT_MAP_PNG_PATH)
    parser.add_argument("--nav-pose", default=DEFAULT_NAV_POSE_PATH)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    rclpy.init()
    node = NavNode(args)
    log.info("nav bridge starting in %s mode%s", node.mode, f" on {node.map_name}" if node.map_name else "")
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.server.stop()
        node.destroy_node()
        rclpy.try_shutdown()
    log.info("nav bridge stopped")


if __name__ == "__main__":
    main()
