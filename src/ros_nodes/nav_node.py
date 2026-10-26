#!/usr/bin/env python3
"""ROS nav bridge node: maps, places, relocalization and the `nav` hub source.

robot-nav.service runs slam_toolbox (mapping) or map_server + AMCL (localize),
picked by nav.json. This node sits next to it and is the only thing that writes
nav.json: the dashboard and the voice tools talk to it over
/run/robot-pet/nav.sock, and it restarts robot-nav to switch modes.

Twice a second it publishes a nav_update to the hub: mode, map, pose (TF
map -> base_link), the saved maps, places and barriers, and whether it wants
the motor rail. It writes /map as /run/robot-pet/map.png for the dashboard.

Barriers are lines drawn on the dashboard that Nav2 must treat as walls (the
steps down to the dining room). They live in barriers.json per map; this node
draws the current map's as a keepout mask (robot_nav.render_keepout_mask) and
hands it to Nav2's filter_mask_server with load_map, so a change applies to the
next plan without a restart. nav.launch.py reads the same file at start.
The gamepad never meets Nav2, so while the map pose is known this node also
publishes /barrier_clearance, the body's room to the nearest barrier ahead and
behind, ten times a second; robot-motion's safety gate stops short of a line
the way it stops short of a lidar return.

Localize mode has to tell AMCL where the robot is. Whenever AMCL becomes
active (boot, mode switch, a redeploy of robot-nav) the bridge either sends
the pose it remembered in /run/robot-pet/nav-pose.json (same map, robot has
not been moved since) or relocalizes: asks for the motor rail, waits for
scans, and for each saved map loads it, scatters the particles and spins the
robot with a `turn` intent, twice if the first spin is not enough. The first
map where the scan fits at AMCL's best guess wins; that guess is handed back
to AMCL as the initial pose so the particles collapse onto it. Saving a map
goes straight to localize mode on that map with slam's last pose, so no spin
is needed.

AMCL's own confidence (the pose covariance) is not used: on the floor it sat
right at any threshold at the true pose and flickered, and a robot carried
to another room keeps a confident wrong pose, because AMCL only grades its
particles against each other. So each tick the bridge scores how well the
latest scan fits the map from the pose TF gives the lidar (robot_nav.scan_fit)
and reads it through one rule (robot_nav.localized_change): a run of bad fits
means lost, the remembered pose is dropped and a trip in progress is
cancelled; a run of good fits means found. A navigate_to on a robot that is
not localized relocalizes first, then goes.

Navigation goals (`navigate_to`, `nav_cancel` on the socket) go to Nav2's
NavigateToPose action. nav.launch.py starts the navigation servers in both
modes but does not activate them: their costmaps need the map frame, and the
lidar that makes it follows the motor rail, so this node brings them up once
the robot is scanning and has a map pose, and pauses them again a few seconds
after the scans stop (see manage_nav2). A goal asks for the rail (through
motor_power_requested) and is sent once the lidar and Nav2 are up. One goal
at a time; a new one replaces it. The goal is cancelled when robot-motion
raises /nav_lock (a voice intent or a stop), when the gamepad moves, or on a
mode switch. The hub message carries the goal and its state.
"""

import argparse
import json
import math
import subprocess
import threading
import time
from pathlib import Path

import rclpy
from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, Twist
from lifecycle_msgs.srv import GetState
from nav2_msgs.action import NavigateToPose
from nav2_msgs.srv import LoadMap, ManageLifecycleNodes
from nav_msgs.msg import OccupancyGrid
from rclpy.action import ActionClient
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import LaserScan
from std_msgs.msg import Bool, Float32MultiArray
from std_srvs.srv import Empty
from tf2_ros import Buffer, TransformException, TransformListener

import robot_nav
from control.motion_intent import request_motion_intent
from control.odometry import normalize_angle
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
# a navigate_to waits this long for the lidar and Nav2 before it fails
GOAL_START_SECONDS = 30.0
# how often at most to ask the lifecycle manager to bring Nav2 up or pause it
NAV2_MANAGE_RETRY_SECONDS = 15.0
# pause Nav2 this long after the last scan: without scans the map -> odom
# transform stops moving forward, and once it is older than TF's 10 s cache
# every map -> base_link lookup fails and planner_server logs an error a second
NAV2_PAUSE_SECONDS = 5.0
SERVICE_WAIT_SECONDS = 10.0
# the safety gate stops 0.2 m short of a barrier; at gamepad speed that is a 4 cm step
BARRIER_CLEARANCE_HZ = 10
# 360 turns per map before relocalize gives up on it (each takes about 8 s)
RELOCALIZE_SPINS = 2
# The covariance AMCL starts from when we hand it a pose: a remembered pose TF
# gave us half a second before the restart, or AMCL's own best guess after a
# relocalize spin, so it is tight and the particles gather around it.
INITIAL_POSE_XY_VARIANCE = 0.1
INITIAL_POSE_YAW_VARIANCE = 0.07
# No scan fit on a tick where the lidar turned more than this since the last:
# a scan taken while spinning at gamepad speed is smeared by several degrees.
SCAN_FIT_MAX_TURN_RADIANS = 0.2


class NavNode(Node):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("robot_nav_bridge")
        self.args = args
        self.nav = robot_nav.load_nav_config(args.nav_config)
        self.mode, self.map_name = robot_nav.resolve_mode(self.nav, args.maps_dir)
        self.places = robot_nav.load_places(args.places)
        self.barriers = robot_nav.load_barriers(args.barriers)
        self.localized = False
        self.activity: str | None = None
        self.motor_power_requested = False
        # best known pose in the map frame: TF when it resolves, else remembered
        self.pose: dict[str, float] | None = None
        self.remembered_pose_written: dict | None = None
        # map_info describes the grid the dashboard has (latest_map plus a serial)
        self.map_info: dict | None = None
        self.map_serial = 0
        self.latest_map: OccupancyGrid | None = None
        self.map_rendered: OccupancyGrid | None = None
        self.map_png_time = 0.0
        self.latest_scan: LaserScan | None = None
        self.last_scan_time: float | None = None
        self.scan_fit: float | None = None
        self.low_fit_ticks = 0
        self.high_fit_ticks = 0
        self.scan_fit_yaw: float | None = None
        # AMCL's best guess, for a relocalize to hand back once the scan fits there
        self.amcl_pose: dict[str, float] | None = None
        # the AMCL lifecycle state the timer last saw; None until the first tick
        self.amcl_active: bool | None = None
        # one localize or save at a time
        self.busy = threading.Lock()
        # the current or last navigation goal (see nav_update); the serial
        # tells a late Nav2 callback for an old goal from the current one
        self.goal: dict | None = None
        self.goal_serial = 0
        self.goal_handle = None
        # a goal waiting for the lidar and Nav2: (serial, x, y, yaw, asked_at),
        # and the serial of the goal a relocalize was already run for
        self.goal_request: tuple | None = None
        self.goal_relocalized_serial = 0
        self.nav_lock = False
        # bt_navigator's lifecycle label (None while it is not up), and our
        # last lifecycle request: the label it was sent from, when, and its
        # pending reply. The servers pass through "inactive" while a bringup
        # runs, so no new request goes out until the reply is in (or it is
        # NAV2_MANAGE_RETRY_SECONDS old), and a refused one waits as long.
        self.nav2_state: str | None = None
        self.nav2_managed_state: str | None = None
        self.nav2_manage_time = 0.0
        self.nav2_manage_future = None

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.create_subscription(OccupancyGrid, "/map", self.on_map, MAP_QOS)
        self.create_subscription(PoseWithCovarianceStamped, "/amcl_pose", self.on_amcl_pose, 10)
        self.create_subscription(LaserScan, "/scan", self.on_scan, qos_profile_sensor_data)
        self.initial_pose_publisher = self.create_publisher(PoseWithCovarianceStamped, "/initialpose", 10)
        self.amcl_state_client = self.create_client(GetState, "/amcl/get_state")
        self.load_map_client = self.create_client(LoadMap, "/map_server/load_map")
        self.filter_mask_client = self.create_client(LoadMap, "/filter_mask_server/load_map")
        self.global_localization_client = self.create_client(Empty, "/reinitialize_global_localization")
        self.nav_client = ActionClient(self, NavigateToPose, "navigate_to_pose")
        self.bt_navigator_state_client = self.create_client(GetState, "/bt_navigator/get_state")
        self.nav2_manage_client = self.create_client(ManageLifecycleNodes, "/lifecycle_manager_navigation/manage_nodes")
        self.create_subscription(Bool, "/nav_lock", self.on_nav_lock, 10)
        self.create_subscription(Twist, "/cmd_vel_teleop", self.on_teleop, 10)
        self.create_timer(0.5, self.tick)
        self.barrier_clearance_publisher = self.create_publisher(Float32MultiArray, "/barrier_clearance", 10)
        self.create_timer(1 / BARRIER_CLEARANCE_HZ, self.publish_barrier_clearance)
        self.server = robot_nav.NavSocketServer(args.nav_socket, self.handle_request)
        self.server.start()
        if self.mode == "localize":
            self.update_keepout()

    # --- ROS callbacks ---------------------------------------------------

    def on_map(self, grid: OccupancyGrid) -> None:
        self.latest_map = grid

    def on_amcl_pose(self, message: PoseWithCovarianceStamped) -> None:
        position, rotation = message.pose.pose.position, message.pose.pose.orientation
        self.amcl_pose = {"x": position.x, "y": position.y, "yaw": robot_nav.yaw_from_quaternion(rotation.x, rotation.y, rotation.z, rotation.w)}

    def on_scan(self, scan: LaserScan) -> None:
        self.latest_scan = scan
        self.last_scan_time = time.monotonic()

    def on_nav_lock(self, message: Bool) -> None:
        self.nav_lock = message.data
        # our own relocalize spin raises the lock too; it must not cancel the
        # trip it is clearing the way for
        if message.data and self.activity != "localizing":
            self.cancel_goal("stopped")

    def on_teleop(self, twist: Twist) -> None:
        if twist.linear.x != 0.0 or twist.angular.z != 0.0:
            self.cancel_goal("gamepad")

    def on_bt_navigator_state(self, future) -> None:
        self.nav2_state = future.result().current_state.label

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
        if self.bt_navigator_state_client.service_is_ready():
            self.bt_navigator_state_client.call_async(GetState.Request()).add_done_callback(self.on_bt_navigator_state)
        else:
            self.nav2_state = None
        self.update_pose()
        self.update_localized()
        self.manage_nav2()
        self.send_pending_goal()
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

    def update_localized(self) -> None:
        """One rule for `localized` each tick, from runs of the scan fit
        (robot_nav.localized_change). AMCL keeps estimating after a relocalize
        gives up or a remembered pose turns out wrong: a drive with the gamepad
        can converge it, or lose it. While a relocalize runs, that thread owns
        the flag."""
        self.scan_fit = self.measure_scan_fit()
        self.low_fit_ticks = self.low_fit_ticks + 1 if self.scan_fit is not None and self.scan_fit < robot_nav.SCAN_FIT_LOST else 0
        self.high_fit_ticks = self.high_fit_ticks + 1 if self.scan_fit is not None and self.scan_fit >= robot_nav.SCAN_FIT_FOUND else 0
        if self.mode != "localize" or self.activity is not None:
            return
        change = robot_nav.localized_change(self.localized, self.low_fit_ticks, self.high_fit_ticks)
        if change is None:
            return
        self.localized, why = change
        log.info("%s: %s (scan fit %s)", "localized" if self.localized else "lost", why, "none" if self.scan_fit is None else f"{self.scan_fit:.2f}")
        if not self.localized:
            # a restart must not trust the old pose, and a trip on it would go
            # wrong. A goal still waiting to start relocalizes first instead.
            Path(self.args.nav_pose).unlink(missing_ok=True)
            self.remembered_pose_written = None
            if self.goal_request is None:
                self.cancel_goal("not_localized")

    def measure_scan_fit(self) -> float | None:
        """How well the latest scan fits the saved map from the lidar's TF pose
        at the scan's time, 0..1, or None when there is nothing to say: mapping
        mode, no fresh scan, no map pose yet, or the robot is turning."""
        scan = self.latest_scan
        if self.mode != "localize" or scan is None or self.map_info is None or not self.scanning():
            return None
        try:
            transform = self.tf_buffer.lookup_transform("map", scan.header.frame_id, Time.from_msg(scan.header.stamp)).transform
        except TransformException:
            return None
        rotation = transform.rotation
        yaw = robot_nav.yaw_from_quaternion(rotation.x, rotation.y, rotation.z, rotation.w)
        turned = self.scan_fit_yaw is not None and abs(normalize_angle(yaw - self.scan_fit_yaw)) > SCAN_FIT_MAX_TURN_RADIANS
        self.scan_fit_yaw = yaw
        if turned:
            return None
        laser_pose = {"x": transform.translation.x, "y": transform.translation.y, "yaw": yaw}
        # map_rendered is the grid map_info describes (latest_map may be 2 s newer)
        fit = robot_nav.scan_fit(
            self.map_rendered.data, self.map_info, laser_pose, scan.ranges, scan.angle_min, scan.angle_increment, scan.range_min, scan.range_max
        )
        return None if fit is None else round(fit, 2)

    def update_map_png(self) -> None:
        grid = self.latest_map
        if grid is None or grid is self.map_rendered or time.monotonic() - self.map_png_time < MAP_PNG_MIN_INTERVAL:
            return
        self.map_rendered = grid
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
            "scan_fit": self.scan_fit,
            "places": self.places,
            "motor_power_requested": self.motor_power_requested or self.navigating(),
            "goal": self.goal,
            "barriers": self.barriers,
        }

    # --- localize mode -----------------------------------------------------

    def on_amcl_became_active(self) -> None:
        remembered = self.remembered_pose()
        if remembered is not None:
            log.info("AMCL active: sending the remembered pose on %s", self.map_name)
            self.set_initial_pose(remembered)
            return
        log.info("AMCL active with no remembered pose: relocalizing")
        self.start_relocalize()

    def start_relocalize(self) -> dict:
        """Relocalize over every saved map, the current one first."""
        maps = robot_nav.maps_to_try(robot_nav.saved_maps(self.args.maps_dir), self.map_name)
        return self.start_activity("localizing", self.relocalize, maps)

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
        # we no longer know where we are, so a restart must not trust the old pose
        Path(self.args.nav_pose).unlink(missing_ok=True)
        self.remembered_pose_written = None
        self.motor_power_requested = True
        try:
            if not self.wait_for_scans():
                log.warning("relocalize: no scans after %.0fs, giving up", SCAN_WAIT_SECONDS)
                return
            for name in maps:
                if self.try_map(name):
                    log.info("relocalize: found the robot on %s", name)
                    self.map_name = name
                    robot_nav.save_nav_config("localize", name, self.args.nav_config)
                    self.update_keepout()
                    self.localized = True
                    return
            log.warning("relocalize: no saved map converged (%s)", ", ".join(maps) or "no maps")
            if maps and maps[-1] != self.map_name:
                # map_server is still showing the last map we tried
                self.load_map(self.map_name)
        finally:
            self.motor_power_requested = False

    def wait_for_scans(self) -> bool:
        deadline = time.monotonic() + SCAN_WAIT_SECONDS
        while time.monotonic() < deadline:
            if self.scanning():
                return True
            time.sleep(0.2)
        return False

    def scanning(self) -> bool:
        return self.scan_age() < 1.0

    def scan_age(self) -> float:
        return math.inf if self.last_scan_time is None else time.monotonic() - self.last_scan_time

    def load_map(self, name: str) -> bool:
        """Hand a saved map to map_server, which publishes it to AMCL and the costmaps."""
        if not self.load_map_client.wait_for_service(SERVICE_WAIT_SECONDS):
            log.warning("load_map %s: map_server not available", name)
            return False
        request = LoadMap.Request()
        request.map_url = robot_nav.map_yaml_path(name, self.args.maps_dir)
        try:
            response = self.load_map_client.call(request, timeout_sec=SERVICE_WAIT_SECONDS)
        except TimeoutError:
            log.warning("load_map %s: map_server did not answer", name)
            return False
        if response.result != LoadMap.Response.RESULT_SUCCESS:
            log.warning("load_map %s failed with result %d", name, response.result)
            return False
        return True

    def try_map(self, name: str) -> bool:
        log.info("relocalize: trying %s", name)
        if not self.load_map(name):
            return False
        try:
            self.global_localization_client.call(Empty.Request(), timeout_sec=SERVICE_WAIT_SECONDS)
        except TimeoutError:
            log.warning("relocalize: AMCL did not answer")
            return False
        self.amcl_pose = None
        # One spin usually leaves AMCL with a few candidate spots a metre apart;
        # a second spin from the same cloud settles it more often than not.
        for spin in range(RELOCALIZE_SPINS):
            result = request_motion_intent(self.args.motion_intent_socket, "turn", degrees=360)
            if not result.get("ok"):
                log.warning("relocalize: turn failed: %s", result.get("error"))
                break
            # the last scan after the spin stops still updates the filter, and
            # the 0.5 s tick needs a scan taken standing still to measure the fit
            time.sleep(1.5)
            fit = self.scan_fit
            if fit is not None and fit >= robot_nav.SCAN_FIT_FOUND and self.amcl_pose is not None:
                # A spread-out cloud never gets tight on its own from spins in
                # place, but its mean is right when the scan fits there, so
                # hand that pose back and let the particles gather around it.
                log.info("relocalize: %s fits (scan fit %.2f) after spin %d", name, fit, spin + 1)
                self.set_initial_pose(self.amcl_pose)
                return True
            log.info("relocalize: %s does not fit yet after spin %d (scan fit %s)", name, spin + 1, "none" if fit is None else f"{fit:.2f}")
        return False

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
            if not request.get("map"):
                return self.start_relocalize()
            if request["map"] not in robot_nav.saved_maps(self.args.maps_dir):
                return {"ok": False, "error": "unknown_map"}
            return self.start_activity("localizing", self.relocalize, [request["map"]])
        if cmd == "create_place":
            return self.create_place(request.get("name"))
        if cmd == "delete_place":
            return self.delete_place(request.get("name"))
        if cmd == "create_barrier":
            return self.create_barrier(request)
        if cmd == "delete_barrier":
            return self.delete_barrier(request.get("index"))
        if cmd == "navigate_to":
            return self.navigate_to(request)
        if cmd == "nav_cancel":
            if not self.navigating():
                return {"ok": True, "accepted": False, "reason": "no_goal"}
            self.cancel_goal("requested")
            return {"ok": True, "accepted": True}
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
        self.amcl_pose = None
        self.cancel_goal("mode_change")
        if mode == "localize":
            self.update_keepout()  # nav.launch.py reads the mask file
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

    def delete_place(self, name) -> dict:
        if name not in self.places:
            return {"ok": False, "error": "unknown_place"}
        del self.places[name]
        robot_nav.save_places(self.places, self.args.places)
        log.info("deleted place %s", name)
        return {"ok": True}

    # --- barriers --------------------------------------------------------------

    def create_barrier(self, request: dict) -> dict:
        if not robot_nav.valid_barrier(request):
            return {"ok": False, "error": "bad_barrier"}
        if self.mode != "localize":
            return {"ok": False, "error": "not_localize_mode"}
        barrier = {key: round(float(request[key]), 2) for key in ("x1", "y1", "x2", "y2")}
        self.barriers.setdefault(self.map_name, []).append(barrier)
        robot_nav.save_barriers(self.barriers, self.args.barriers)
        self.update_keepout()
        log.info("added a barrier on %s", self.map_name)
        return {"ok": True, "barrier": barrier}

    def delete_barrier(self, index) -> dict:
        barriers = self.barriers.get(self.map_name, [])
        if not isinstance(index, int) or not 0 <= index < len(barriers):
            return {"ok": False, "error": "unknown_barrier"}
        del barriers[index]
        robot_nav.save_barriers(self.barriers, self.args.barriers)
        self.update_keepout()
        log.info("deleted a barrier on %s", self.map_name)
        return {"ok": True}

    def update_keepout(self) -> None:
        """Draw this map's barriers as the keepout mask and hand it to Nav2's
        filter_mask_server if it is up (nav.launch.py reads the file otherwise)."""
        stem = Path(self.args.keepout_dir) / self.map_name
        robot_nav.render_keepout_mask(self.barriers.get(self.map_name, []), str(stem))
        if self.filter_mask_client.service_is_ready():
            request = LoadMap.Request()
            request.map_url = f"{stem}.yaml"
            self.filter_mask_client.call_async(request).add_done_callback(self.on_keepout_loaded)

    def publish_barrier_clearance(self) -> None:
        """Tell robot-motion how much room the body has to the nearest barrier,
        while there is a map pose. Nothing is sent in mapping mode or before
        AMCL has a pose, and the safety gate lets a stale value lapse. A pose
        that is not trusted (localized no) still counts: a wrong stop near a
        line beats driving over one while the flag is making its mind up."""
        if self.mode != "localize":
            return
        self.update_pose()
        if self.pose is None:
            return
        forward_m, rear_m = robot_nav.barrier_clearances(self.pose, self.barriers.get(self.map_name, []))
        self.barrier_clearance_publisher.publish(Float32MultiArray(data=[forward_m, rear_m]))

    def on_keepout_loaded(self, future) -> None:
        if future.result().result != LoadMap.Response.RESULT_SUCCESS:
            log.warning("filter_mask_server did not load the keepout mask (result %d)", future.result().result)

    # --- navigation goals ----------------------------------------------------

    def navigating(self) -> bool:
        return self.goal is not None and self.goal["state"] == "navigating"

    def manage_nav2(self) -> None:
        """Keep the Nav2 navigation servers active while the lidar scans and
        paused while it does not. Their costmaps need the map frame, which only
        the lidar provides, and the lidar follows the motor rail: so Nav2 comes
        up for each trip and pauses a few seconds after the rail goes off."""
        if not self.nav2_manage_client.service_is_ready():
            return
        # A reply from a manager that restarted meanwhile (mode switch, redeploy)
        # never comes, so an old request only holds the next one off for so long.
        waiting = self.nav2_manage_future is not None and not self.nav2_manage_future.done()
        if (waiting or self.nav2_state == self.nav2_managed_state) and time.monotonic() - self.nav2_manage_time < NAV2_MANAGE_RETRY_SECONDS:
            return
        if self.nav2_state == "active":
            if self.scan_age() > NAV2_PAUSE_SECONDS:
                self.cancel_goal("lidar_unavailable")
                self.send_nav2_command("pausing", ManageLifecycleNodes.Request.PAUSE)
        elif self.nav2_state == "unconfigured" and self.scanning() and self.pose is not None:
            self.send_nav2_command("bringing up", ManageLifecycleNodes.Request.STARTUP)
        elif self.nav2_state == "inactive" and self.scanning() and self.pose is not None:
            self.send_nav2_command("resuming", ManageLifecycleNodes.Request.RESUME)

    def send_nav2_command(self, what: str, command: int) -> None:
        self.nav2_managed_state = self.nav2_state
        self.nav2_manage_time = time.monotonic()
        log.info("%s the Nav2 navigation servers", what)
        self.nav2_manage_future = self.nav2_manage_client.call_async(ManageLifecycleNodes.Request(command=command))
        self.nav2_manage_future.add_done_callback(self.on_nav2_managed)

    def on_nav2_managed(self, future) -> None:
        if not future.result().success:
            log.warning("the Nav2 lifecycle manager refused (see robot-nav); retrying in %.0fs", NAV2_MANAGE_RETRY_SECONDS)

    def navigate_to(self, request: dict) -> dict:
        """Send a NavigateToPose goal for a saved place or a map x, y."""
        place_name = request.get("place")
        if place_name is not None:
            place = self.places.get(place_name)
            if place is None:
                return {"ok": False, "error": "unknown_place", "places": robot_nav.places_on_map(self.places, self.map_name)}
            if place["map"] != self.map_name:
                return {"ok": False, "error": "place_on_other_map", "map": place["map"]}
            x, y, yaw = place["x"], place["y"], place["yaw"]
        else:
            x, y, yaw = request.get("x"), request.get("y"), request.get("yaw")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                return {"ok": False, "error": "bad_goal"}
            if yaw is None:
                # no heading asked for: arrive facing the way we came
                yaw = robot_nav.yaw_toward(self.pose["x"], self.pose["y"], x, y) if self.pose else 0.0
        if self.busy.locked():
            return {"ok": True, "accepted": False, "reason": self.activity or "busy"}
        if self.nav_lock:
            return {"ok": True, "accepted": False, "reason": "wheels_busy"}

        self.cancel_goal("replaced")
        self.goal_serial += 1
        self.goal = {"target": place_name, "x": float(x), "y": float(y), "state": "navigating", "remaining_m": None}
        # navigating() asks for the motor rail; the lidar follows the rail and
        # Nav2 follows the lidar, so tick() sends the goal once both are up,
        # after a relocalize if the robot does not know where it is
        self.goal_request = (self.goal_serial, self.map_name, float(x), float(y), yaw, time.monotonic())
        log.info("navigating to %s (%.2f, %.2f)", place_name or "a point", x, y)
        return {"ok": True, "accepted": True, "goal": self.goal}

    def send_pending_goal(self) -> None:
        if self.goal_request is None:
            return
        serial, map_name, x, y, yaw, asked_at = self.goal_request
        if self.activity is not None:
            # a relocalize (or save) is running; the start clock runs from its end
            self.goal_request = (serial, map_name, x, y, yaw, time.monotonic())
            return
        if self.mode == "localize" and not self.localized:
            if self.goal_relocalized_serial == serial:
                self.goal_request = None
                self.end_goal(serial, "failed", "not_localized")
            elif self.start_relocalize()["accepted"]:
                self.goal_relocalized_serial = serial
                log.info("navigating: not localized, relocalizing first")
            return
        if map_name != self.map_name:
            # the relocalize found us on another map; the goal's x, y mean nothing here
            self.goal_request = None
            self.end_goal(serial, "failed", "place_on_other_map")
            return
        if time.monotonic() - asked_at > GOAL_START_SECONDS:
            self.goal_request = None
            self.end_goal(serial, "failed", "nav2_unavailable" if self.scanning() else "lidar_unavailable")
            return
        if not self.scanning() or self.nav2_state != "active" or not self.nav_client.server_is_ready():
            return
        self.goal_request = None

        goal = NavigateToPose.Goal()
        goal.pose.header.frame_id = "map"
        goal.pose.header.stamp = self.get_clock().now().to_msg()
        goal.pose.pose.position.x = float(x)
        goal.pose.pose.position.y = float(y)
        goal.pose.pose.orientation.z = math.sin(yaw / 2)
        goal.pose.pose.orientation.w = math.cos(yaw / 2)
        future = self.nav_client.send_goal_async(goal, feedback_callback=lambda message: self.on_goal_feedback(serial, message))
        future.add_done_callback(lambda done: self.on_goal_response(serial, done))

    def on_goal_response(self, serial: int, future) -> None:
        handle = future.result()
        if serial != self.goal_serial or not self.navigating():
            # cancelled or replaced before Nav2 even answered; don't let it run
            if handle.accepted:
                handle.cancel_goal_async()
            return
        if not handle.accepted:
            self.end_goal(serial, "failed", "rejected")
            return
        self.goal_handle = handle
        handle.get_result_async().add_done_callback(lambda done: self.on_goal_result(serial, done))

    def on_goal_feedback(self, serial: int, message) -> None:
        if serial == self.goal_serial and self.navigating():
            self.goal["remaining_m"] = round(message.feedback.distance_remaining, 2)

    def on_goal_result(self, serial: int, future) -> None:
        result = future.result()
        if result.status == GoalStatus.STATUS_SUCCEEDED:
            self.end_goal(serial, "arrived")
        elif result.status == GoalStatus.STATUS_CANCELED:
            self.end_goal(serial, "cancelled")
        else:
            self.end_goal(serial, "failed", result.result.error_msg or f"status {result.status}")

    def end_goal(self, serial: int, state: str, reason: str | None = None) -> None:
        """Mark the goal over, unless it was already (a cancel beat Nav2's result)."""
        if serial != self.goal_serial or not self.navigating():
            return
        self.goal["state"] = state
        if reason:
            self.goal["reason"] = reason
        self.goal_handle = None
        log.info("navigation %s%s", state, f": {reason}" if reason else "")

    def cancel_goal(self, reason: str) -> None:
        if not self.navigating():
            return
        self.goal_request = None
        if self.goal_handle is not None:
            self.goal_handle.cancel_goal_async()
        self.end_goal(self.goal_serial, "cancelled", reason)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot nav bridge ROS node.")
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET)
    parser.add_argument("--nav-socket", default=DEFAULT_NAV_SOCKET)
    parser.add_argument("--motion-intent-socket", default=DEFAULT_MOTION_INTENT_SOCKET)
    parser.add_argument("--nav-config", default=robot_nav.DEFAULT_NAV_CONFIG_PATH)
    parser.add_argument("--places", default=robot_nav.DEFAULT_PLACES_PATH)
    parser.add_argument("--maps-dir", default=robot_nav.DEFAULT_MAPS_DIR)
    parser.add_argument("--barriers", default=robot_nav.DEFAULT_BARRIERS_PATH)
    parser.add_argument("--keepout-dir", default=robot_nav.DEFAULT_KEEPOUT_DIR)
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
