#!/usr/bin/env python3
"""ROS nav bridge node: maps, places, localization and the `nav` hub source.

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
not been moved since) or searches. The search (robot_nav.global_search) runs
on its own thread on the latest scan: it scores the scan at every pose on
every saved map, and a clear winner named by two searches in a row is handed
to AMCL as the initial pose (loading its map first if it is another floor).
It runs back to back while the robot does not know where it is, and every
search_interval_s while it does, to catch a slip: a clearly better place
named by two searches in a row (a rival) fails every verdict until a search
names none, so the pose drops. The robot never
moves itself to search. Saving a map goes straight to localize mode on that
map with slam's last pose.

AMCL's own confidence (the pose covariance) is not used: on the floor it sat
right at any threshold at the true pose and flickered, and a robot carried
to another room keeps a confident wrong pose, because AMCL only grades its
particles against each other. So each tick a judge asks whether the latest
scan contradicts the map from AMCL's pose (robot_nav.beam_check), and one
state, read by everyone, says how far the pose is trusted:

    tracking   passed the judge and the robot has driven a little since the
               pose was set: trips go, places can be saved
    checking   a pose not yet confirmed by a drive; trips go once its latest
               verdict passed ("clean")
    searching  no pose, or the judge dropped it after a run of failed scans
               (the remembered pose goes and a trip in progress is cancelled)
    stale      the lidar is off; a pose held through it is checked afresh

`localized` is tracking or clean checking. A navigate_to while searching
waits for the search, then goes.

A scan the map disagrees with gets a second opinion, the self-check
(robot_nav.scan_check): it is compared with the last scan that passed,
carried by the wheel odometry. If they match, the room has changed there
(a moved table) and the verdict does not count against the pose. A slide or
a turn the wheels did not see does not match.

The bridge also notices the robot in someone's hands (update_handling). While
the robot is not driving, both cliff sensors seeing no floor means picked up,
and the IMU rocking means pushed. While it drives the cliff sensors stay
cliff sensors, and only a tip from how it sat means picked up. Either way
the pose drops to checking, the search and the judge wait, and once the
floor is back under both cliff sensors and nothing has rocked for a moment
(put down) the pose is checked afresh and a search runs straight away.

The bars and switches are in localization.json (config/localization.py). The
`record` command saves scans and poses for scripts/localization-replay.py.

Navigation goals (`navigate_to`, `nav_cancel` on the socket) go to Nav2's
NavigateToPose action. nav.launch.py starts the navigation servers in both
modes but does not activate them: their costmaps need the map frame, and the
lidar that makes it follows the motor rail, so this node brings them up once
the robot is scanning and has a map pose, and pauses them again a few seconds
after the scans stop (see manage_nav2). A goal asks for the rail (through
motor_power_requested) and is sent once the lidar, the pose and Nav2 are up. One goal
at a time; a new one replaces it. The goal is cancelled when robot-motion
raises /nav_lock (a voice intent or a stop), when the gamepad moves, or on a
mode switch. The hub message carries the goal and its state.
"""

import argparse
import json
import math
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import rclpy
from action_msgs.msg import GoalStatus
from action_msgs.srv import CancelGoal
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, Twist
from lifecycle_msgs.srv import GetState
from nav2_msgs.action import NavigateToPose
from nav2_msgs.srv import LoadMap, ManageLifecycleNodes
from nav_msgs.msg import OccupancyGrid
from rclpy.action import ActionClient
from rclpy.duration import Duration
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import Imu, LaserScan, Range
from std_msgs.msg import Bool, Float32MultiArray
from tf2_ros import Buffer, TransformException, TransformListener

import robot_nav
from config.localization import DEFAULT_CONFIG_PATH as DEFAULT_LOCALIZATION_SETTINGS_PATH
from config.localization import LocalizationSettings, LocalizationSettingsError, load_localization_settings
from config.sensors import DEFAULT_CONFIG_PATH as DEFAULT_SENSORS_CONFIG_PATH
from config.sensors import SensorsConfigError, cliff_trip_mm, load_sensors_config
from control.odometry import normalize_angle
from drivers.imu import quaternion_to_euler_degrees
from lib.log import setup_logging
from telemetry.messages import nav_update
from telemetry.paths import (
    DEFAULT_MAP_PNG_PATH,
    DEFAULT_NAV_POSE_PATH,
    DEFAULT_NAV_SOCKET,
    DEFAULT_PUBLISH_SOCKET,
)
from telemetry.socket_client import publish_message

log = setup_logging("robot-nav-bridge")

# slam_toolbox and map_server both latch /map (transient local, reliable).
MAP_QOS = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL, reliability=ReliabilityPolicy.RELIABLE)
MAP_PNG_MIN_INTERVAL = 2.0
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
# The covariance AMCL starts from when we hand it a pose: a remembered pose TF
# gave us half a second before the restart, or the search's winner, so it is
# tight (about 14 cm and 6 degrees) and the particles gather around it.
INITIAL_POSE_XY_VARIANCE = 0.02
INITIAL_POSE_YAW_VARIANCE = 0.01
# the judge ignores this long after we hand AMCL a pose, while AMCL takes it
SEED_SETTLE_SECONDS = 1.0
# A new bridge under a running AMCL waits this long for AMCL's map pose to
# reach it over TF before deciding AMCL is fresh and needs a pose from us.
AMCL_POSE_WAIT_SECONDS = 3.0
# No verdict on a tick, and no search on a scan, where the lidar turned more
# than this in the half second before: a scan taken while spinning at gamepad
# speed is smeared by several degrees.
MAX_TURN_RADIANS = 0.2
# while searching, the pause between one search and the next
SEARCH_PAUSE_SECONDS = 0.2
# The robot counts as driving this long after the last moving /cmd_vel (the
# gamepad, Nav2) or /nav_lock (a voice drive), and rocking counts only from
# then on: on the floor a gamepad stop wobbled the robot for up to 1 s.
DRIVE_HOLD_SECONDS = 1.5
# a cliff sensor or the IMU silent this long takes no part in noticing handling
SENSOR_STALE_SECONDS = 1.0
# a push is the robot rocking this long or less
NUDGE_WINDOW_SECONDS = 1.0
# Handling ends after this long even if a cliff sensor still sees no floor,
# so one stuck sensor cannot hold the pose forever. A robot still in the air
# is noticed as picked up again on the next tick.
HANDLED_MAX_SECONDS = 10.0
# a `record` without seconds records this long, and none longer than the max
RECORD_SECONDS = 10.0
RECORD_MAX_SECONDS = 120.0


class NavNode(Node):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("robot_nav_bridge")
        self.args = args
        self.nav = robot_nav.load_nav_config(args.nav_config)
        self.mode, self.map_name = robot_nav.resolve_mode(self.nav, args.maps_dir)
        self.places = robot_nav.load_places(args.places)
        self.barriers = robot_nav.load_barriers(args.barriers)
        try:
            self.settings = load_localization_settings(args.localization_settings)
        except LocalizationSettingsError as exc:
            log.warning("%s; using the defaults", exc)
            self.settings = LocalizationSettings()
        # how far the pose is trusted (see the module docstring); reported as
        # "stale" while the lidar is off. clean: the latest verdict passed.
        self.pose_state = "searching"
        self.clean = False
        self.passed_scans = 0
        self.failed_scans = 0
        # wheel travel since the pose was set, from odom (x, y) each tick
        self.travelled_m = 0.0
        self.odom_position: dict[str, float] | None = None
        self.seeded_at = 0.0
        self.activity: str | None = None
        # the search thread (see search) and what it last found: the place the
        # last search named (a clear winner while searching, a rival while
        # the pose is held) with the lidar's odom pose then, whether a rival
        # stands, and a summary for the dashboard. pose_lock keeps the tick's
        # verdicts and the search's seeding apart.
        self.search_now = threading.Event()
        self.pose_lock = threading.Lock()
        self.search_grids_cache: dict[str, tuple[float, dict]] = {}
        self.named: dict | None = None
        self.rival_stands = False
        self.search_summary: dict | None = None
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
        # the latest verdict (robot_nav.beam_check)
        self.check: dict | None = None
        self.judged_scan: LaserScan | None = None
        self.judged_yaw: float | None = None
        # The self-check (robot_nav.scan_check): the judged scan per degree
        # with the lidar's odom pose then, the last one that passed (the
        # reference), and how well a failed scan matched it.
        self.judged_view: tuple | None = None
        self.reference: tuple | None = None
        self.scan_agree: float | None = None
        # Noticing the robot handled (see update_handling): when it last drove,
        # each cliff sensor's trip in metres and its latest (no floor, when),
        # the IMU's (when, roll, pitch) over the last second, how it sat before
        # it drove, and whether a cliff met while driving (a stair edge) is
        # still under a sensor. handled is why the pose stopped counting
        # ("picked up", "pushed") until the robot is put down, since handled_at.
        self.drive_time = -math.inf
        self.cliff_trips: dict[str, float] = {}
        try:
            sensors = load_sensors_config(args.sensors_config)
            self.cliff_trips = {entry.name: cliff_trip_mm(entry, sensors.safety) / 1000 for entry in sensors.sensors if entry.role == "cliff"}
        except SensorsConfigError as exc:
            log.warning("%s; not watching the cliff sensors for pick-ups", exc)
        self.cliffs: dict[str, tuple[bool, float]] = {}
        self.tilts: deque = deque()
        self.rest_tilt: tuple[float, float] | None = None
        # a bridge (re)started with the robot parked at a stair edge never saw
        # it drive there, so no floor at start counts as an edge too
        self.cliff_edge = True
        self.handled: str | None = None
        self.handled_at = -math.inf
        self.quiet_since = 0.0
        # AMCL's best guess, for the recordings
        self.amcl_pose: dict[str, float] | None = None
        # a `record` in progress: {"label", "file", "path", "until", "scans"}
        self.recording: dict | None = None
        # the AMCL lifecycle state the timer last saw; None until decided
        self.amcl_active: bool | None = None
        self.started = time.monotonic()
        # one map switch or save at a time
        self.busy = threading.Lock()
        # the current or last navigation goal (see nav_update); the serial
        # tells a late Nav2 callback for an old goal from the current one
        self.goal: dict | None = None
        self.goal_serial = 0
        self.goal_handle = None
        # a goal waiting for the lidar, the pose and Nav2: (serial, map, x, y, yaw, asked_at)
        self.goal_request: tuple | None = None
        # a trip Nav2 was still driving when the last bridge went away
        self.orphan_trip_checked = False
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
        self.nav_client = ActionClient(self, NavigateToPose, "navigate_to_pose")
        self.nav_cancel_client = self.create_client(CancelGoal, "/navigate_to_pose/_action/cancel_goal")
        self.bt_navigator_state_client = self.create_client(GetState, "/bt_navigator/get_state")
        self.nav2_manage_client = self.create_client(ManageLifecycleNodes, "/lifecycle_manager_navigation/manage_nodes")
        self.create_subscription(Bool, "/nav_lock", self.on_nav_lock, 10)
        self.create_subscription(Twist, "/cmd_vel_teleop", self.on_teleop, 10)
        self.create_subscription(Twist, "/cmd_vel", self.on_cmd_vel, 10)
        self.create_subscription(Imu, "/imu", self.on_imu, 10)
        for name in self.cliff_trips:
            self.create_subscription(Range, f"/range/{name}", lambda message, name=name: self.on_cliff(name, message), 10)
        self.create_timer(0.5, self.tick)
        self.barrier_clearance_publisher = self.create_publisher(Float32MultiArray, "/barrier_clearance", 10)
        self.create_timer(1 / BARRIER_CLEARANCE_HZ, self.publish_barrier_clearance)
        self.server = robot_nav.NavSocketServer(args.nav_socket, self.handle_request)
        self.server.start()
        if self.mode == "localize":
            self.update_keepout()
        threading.Thread(target=self.search_loop, name="nav-search", daemon=True).start()

    # --- ROS callbacks ---------------------------------------------------

    def on_map(self, grid: OccupancyGrid) -> None:
        self.latest_map = grid

    def on_amcl_pose(self, message: PoseWithCovarianceStamped) -> None:
        position, rotation = message.pose.pose.position, message.pose.pose.orientation
        self.amcl_pose = {"x": position.x, "y": position.y, "yaw": robot_nav.yaw_from_quaternion(rotation.x, rotation.y, rotation.z, rotation.w)}

    def on_scan(self, scan: LaserScan) -> None:
        if not scan.ranges:
            return
        self.latest_scan = scan
        self.last_scan_time = time.monotonic()
        if self.recording is not None:
            # the odometry for the scan's time arrives after it, so the tick writes it
            self.recording["scans"].append((scan, self.lookup_pose("map", "odom", Time()), self.amcl_pose, self.state()))

    def on_nav_lock(self, message: Bool) -> None:
        self.nav_lock = message.data
        if message.data:
            self.cancel_goal("stopped")

    def on_teleop(self, twist: Twist) -> None:
        if twist.linear.x != 0.0 or twist.angular.z != 0.0:
            self.cancel_goal("gamepad")

    def on_cmd_vel(self, twist: Twist) -> None:
        if twist.linear.x != 0.0 or twist.angular.z != 0.0:
            self.drive_time = time.monotonic()

    def on_cliff(self, name: str, message: Range) -> None:
        # +inf is nothing in range: no floor. -inf (too close) and nan (a
        # failed read) say nothing; a lifted sensor gives the odd -inf.
        if message.range == math.inf or math.isfinite(message.range):
            self.cliffs[name] = (message.range > self.cliff_trips[name], time.monotonic())

    def on_imu(self, message: Imu) -> None:
        rotation = message.orientation
        roll, pitch, _ = quaternion_to_euler_degrees((rotation.x, rotation.y, rotation.z, rotation.w))
        now = time.monotonic()
        self.tilts.append((now, roll, pitch))
        while self.tilts[0][0] < now - NUDGE_WINDOW_SECONDS:
            self.tilts.popleft()

    def on_bt_navigator_state(self, future) -> None:
        self.nav2_state = future.result().current_state.label

    def on_amcl_state(self, future) -> None:
        active = future.result().current_state.label == "active"
        if active and self.amcl_active is None:
            # Only this bridge restarted? Then AMCL is up and tracking (it gives
            # us a map pose) where the last bridge left it localized, and
            # handing it the remembered pose again would reset its particles,
            # mid-move for all we know. A fresh AMCL gives no pose until it gets
            # one; TF takes a moment to reach a new node, so wait for it.
            if self.pose is not None and self.remembered_pose() is not None:
                log.info("AMCL already tracking on %s: keeping its pose, checking it", self.map_name)
                self.start_checking()
                self.amcl_active = True
                return
            if time.monotonic() - self.started < AMCL_POSE_WAIT_SECONDS:
                return
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
        self.update_travelled()
        self.update_handling()
        self.judge_scan()
        with self.pose_lock:
            self.update_state()
        self.manage_nav2()
        if not self.orphan_trip_checked and self.nav_cancel_client.service_is_ready():
            self.cancel_orphan_trip()
        self.send_pending_goal()
        self.update_map_png()
        self.remember_pose()
        self.write_recording()
        publish_message(self.args.telemetry_socket, nav_update(**self.status()))

    def lookup_pose(self, target_frame: str, source_frame: str, at: Time) -> dict[str, float] | None:
        """source_frame's pose in target_frame from TF, or None if TF cannot say."""
        try:
            transform = self.tf_buffer.lookup_transform(target_frame, source_frame, at).transform
        except TransformException:
            return None
        rotation = transform.rotation
        return {
            "x": transform.translation.x,
            "y": transform.translation.y,
            "yaw": robot_nav.yaw_from_quaternion(rotation.x, rotation.y, rotation.z, rotation.w),
        }

    def update_pose(self) -> None:
        pose = self.lookup_pose("map", "base_link", Time())
        if pose is not None:
            self.pose = pose

    def update_travelled(self) -> None:
        """Add up the wheel travel for the motion gate. Odometry counts the
        wheels, so a robot carried or turned by hand confirms nothing."""
        position = self.lookup_pose("odom", "base_link", Time())
        if position is None:
            return
        if self.odom_position is not None:
            self.travelled_m += math.hypot(position["x"] - self.odom_position["x"], position["y"] - self.odom_position["y"])
        self.odom_position = position

    def update_handling(self) -> None:
        """Notice the robot picked up, pushed or put down. While it drives,
        the cliff sensors are cliff sensors, and only a tip from how it sat
        says picked up. While it does not, both cliff sensors seeing no floor
        says picked up and rocking says pushed. A cliff met while driving
        stays a cliff, not a pick-up, until the floor is back under both.
        The pose waits (start_handling) until the floor is under both cliff
        sensors and nothing has rocked for set_down_s, or HANDLED_MAX_SECONDS
        have passed. A sensor that is missing or silent takes no part."""
        now = time.monotonic()
        if self.nav_lock:
            self.drive_time = now
        driving = now - self.drive_time < DRIVE_HOLD_SECONDS
        no_floor = [far for far, at in self.cliffs.values() if now - at < SENSOR_STALE_SECONDS]
        tilts = self.tilts if self.tilts and now - self.tilts[-1][0] < SENSOR_STALE_SECONDS else []
        settled = [(roll, pitch) for at, roll, pitch in tilts if at > self.drive_time + DRIVE_HOLD_SECONDS]
        rolls, pitches = [roll for roll, _ in settled], [pitch for _, pitch in settled]
        rocked = max(max(rolls) - min(rolls), max(pitches) - min(pitches)) if settled else 0.0
        if tilts and not driving:
            self.rest_tilt = tilts[-1][1:]
        tipped = math.hypot(tilts[-1][1] - self.rest_tilt[0], tilts[-1][2] - self.rest_tilt[1]) if tilts and self.rest_tilt else 0.0
        if driving and any(no_floor):
            self.cliff_edge = True
        elif no_floor and not any(no_floor):
            self.cliff_edge = False

        reason = detail = None
        if driving:
            if self.settings.lift_tip and tipped > self.settings.lift_tip_degrees:
                reason, detail = "picked up", f"tipped {tipped:.0f} degrees while driving"
        elif self.settings.lift_cliffs and len(no_floor) == len(self.cliff_trips) > 0 and all(no_floor) and not self.cliff_edge:
            reason, detail = "picked up", "no floor under either cliff sensor"
        elif self.settings.nudge and rocked > self.settings.nudge_degrees:
            reason, detail = "pushed", f"rocked {rocked:.1f} degrees"
        if reason is not None or any(no_floor):
            self.quiet_since = now
        if reason is not None and self.handled is None:
            self.start_handling(reason, detail)
        elif self.handled is not None and (now - self.quiet_since >= self.settings.set_down_s or now - self.handled_at >= HANDLED_MAX_SECONDS):
            if now - self.quiet_since >= self.settings.set_down_s:
                log.info("put down: checking the pose")
            else:
                log.warning("still %s after %.0f s (a cliff sensor stuck?): checking the pose", self.handled, HANDLED_MAX_SECONDS)
            self.handled = None
            with self.pose_lock:
                if self.mode == "localize" and self.pose_state != "searching":
                    self.start_checking()
            self.search_now.set()

    def start_handling(self, reason: str, detail: str) -> None:
        """The robot is in someone's hands: the pose stops counting (checking,
        not clean, no self-check), nothing the search found before counts, no
        verdict or search runs until it is put down, and a trip stops."""
        log.info("%s (%s): the pose waits until the robot is put down", reason, detail)
        self.handled = reason
        self.handled_at = time.monotonic()
        with self.pose_lock:
            self.named = None
            if self.mode == "localize" and self.pose_state != "searching":
                self.start_checking()
        if self.goal_request is None:
            self.cancel_goal(reason)

    def judge_scan(self) -> None:
        """Judge the latest scan (robot_nav.beam_check) from the lidar's map
        pose: the latest map -> odom
        (AMCL post-dates it, so a lookup at the scan's time returns AMCL's
        previous belief) with odom -> lidar at the scan's time. Nothing to say
        (None) in mapping mode, without a new scan or a map pose, or when the
        lidar turned too much since the last tick (from odometry)."""
        scan = self.latest_scan
        map_odom = odom_laser = None
        self.scan_agree = None
        if self.mode == "localize" and scan is not None and scan is not self.judged_scan and self.map_info is not None and self.scanning():
            map_odom = self.lookup_pose("map", "odom", Time())
            odom_laser = self.lookup_pose("odom", scan.header.frame_id, Time.from_msg(scan.header.stamp))
        if map_odom is None or odom_laser is None:
            self.check = None
            return
        turned = self.judged_yaw is not None and abs(normalize_angle(odom_laser["yaw"] - self.judged_yaw)) > MAX_TURN_RADIANS
        self.judged_scan, self.judged_yaw = scan, odom_laser["yaw"]
        if turned:
            self.check = None
            return
        laser_pose = robot_nav.compose_poses(map_odom, odom_laser)
        # map_rendered is the grid map_info describes (latest_map may be 2 s newer)
        self.check = robot_nav.beam_check(
            self.map_rendered.data, self.map_info, laser_pose, scan.ranges, scan.angle_min, scan.angle_increment, scan.range_min, scan.range_max, self.settings
        )
        # a failed scan is held against the last one that passed, carried by the wheels
        self.judged_view = (robot_nav.degree_ranges(scan.ranges, scan.angle_min, scan.angle_increment, scan.range_min, scan.range_max), odom_laser)
        if self.check is not None and not self.check["passes"] and self.reference is not None and self.settings.self_check:
            moved = robot_nav.relative_pose(odom_laser, self.reference[1])
            self.scan_agree = robot_nav.scan_check(self.reference[0], self.judged_view[0], moved, self.settings)

    def update_state(self) -> None:
        """One rule for the pose state each tick, from the judge's verdicts.
        While a rival stands every verdict fails. A failed verdict whose scan
        matches the last one that passed (the self-check) does not count.
        While the map is being switched, that thread owns the state, and while
        the robot is in someone's hands no verdict counts."""
        if self.mode != "localize" or self.activity is not None or self.handled is not None:
            return
        if not self.scanning():
            # stale: the robot may be carried while the lidar is off, so a pose
            # held through it starts over as checking when the scans come back
            if self.pose_state == "tracking" or self.clean:
                self.start_checking()
            return
        if self.check is None or self.pose_state == "searching" or time.monotonic() - self.seeded_at < SEED_SETTLE_SECONDS:
            return
        if self.check["passes"] and not self.rival_stands:
            self.clean = True
            self.passed_scans += 1
            self.failed_scans = 0
            self.reference = self.judged_view
            if self.pose_state == "checking" and self.passed_scans >= self.settings.fail_scans and self.travelled_m >= self.settings.motion_gate_m:
                self.pose_state = "tracking"
                log.info("tracking on %s (%s)", self.map_name, self.check_text())
            return
        if not self.rival_stands and self.scan_agree is not None and self.scan_agree >= self.settings.self_check_min:
            # the map disagrees, but the wheels and the last scan that passed
            # vouch for the pose: the room has changed here (a moved table)
            self.clean = True
            self.failed_scans = 0
            return
        self.clean = False
        self.passed_scans = 0
        self.failed_scans += 1
        if self.failed_scans >= self.settings.fail_scans:
            log.info("lost: %d scans in a row contradict the map (%s)", self.failed_scans, self.check_text())
            self.pose_state = "searching"
            self.search_now.set()
            # a restart must not trust the old pose, and a trip on it would go
            # wrong. A goal still waiting to start waits for the search instead.
            Path(self.args.nav_pose).unlink(missing_ok=True)
            self.remembered_pose_written = None
            if self.goal_request is None:
                self.cancel_goal("lost")

    def start_checking(self) -> None:
        """A new pose (or one held through the lidar being off): checking,
        not yet clean, judged afresh from here."""
        self.pose_state = "checking"
        self.clean = False
        self.rival_stands = False
        self.reference = None
        self.passed_scans = self.failed_scans = 0
        self.travelled_m = 0.0
        self.seeded_at = time.monotonic()

    def state(self) -> str | None:
        if self.mode != "localize":
            return None
        return self.pose_state if self.scanning() else "stale"

    def localized(self) -> bool:
        return self.state() == "tracking" or (self.state() == "checking" and self.clean)

    def check_text(self) -> str:
        check = self.check
        if check is None:
            return "no verdict"
        rival = ", a clearly better place stands" if self.rival_stands else ""
        self_check = "" if self.scan_agree is None else f", self-check {self.scan_agree:.2f}"
        return f"agree {check['agree']:.2f}, through {check['through']:.2f}{self_check}{rival}"

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
        AMCL does not cost a search. /run is cleared on reboot on purpose: a
        powered-off robot may have been carried somewhere else."""
        if not self.localized() or self.pose is None:
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
        check = self.check
        return {
            "mode": self.mode,
            "map_name": self.map_name,
            "maps": robot_nav.saved_maps(self.args.maps_dir),
            "localized": self.localized(),
            "state": self.state(),
            "check": None if check is None else {
                "agree": round(check["agree"], 2),
                "through": round(check["through"], 2),
                "opinion": round(check["opinion"], 2),
                "passes": check["passes"],
                "self_check": None if self.scan_agree is None else round(self.scan_agree, 2),
            },
            "handled": self.handled,
            "search": self.search_summary,
            "recording": None if self.recording is None else self.recording["label"],
            "activity": self.activity,
            "pose": self.pose,
            "map_info": self.map_info,
            "places": self.places,
            "motor_power_requested": self.navigating(),
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
        log.info("AMCL active with no remembered pose: searching")
        self.pose_state = "searching"
        self.clean = False
        self.search_now.set()

    def set_initial_pose(self, pose: dict, stamp: Time | None = None) -> None:
        """Hand AMCL a pose; the state becomes checking. A pose from an earlier
        moment (stamp) is first carried forward by the odometry since then:
        AMCL takes a seed as the pose *now*."""
        then = None if stamp is None else self.lookup_pose("odom", "base_link", stamp)
        now = self.lookup_pose("odom", "base_link", Time())
        if then is not None and now is not None:
            pose = robot_nav.compose_poses(pose, robot_nav.relative_pose(then, now))
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
        self.start_checking()

    def start_activity(self, name: str, target, *args) -> dict:
        """Run target on its own thread unless a save or a map switch is
        already running. Returns the socket ack: accepted, or not because busy."""
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

    # --- the search ----------------------------------------------------------

    def search_loop(self) -> None:
        """The search thread: back to back while searching, every
        search_interval_s while the pose is held, and at once when woken
        (Localize, a dropped pose, AMCL coming up without one)."""
        while True:
            try:
                soon = self.search()
            except Exception:
                log.exception("search failed; trying again")
                soon = False
            self.search_now.wait(SEARCH_PAUSE_SECONDS if soon else self.settings.search_interval_s)
            self.search_now.clear()

    def search(self) -> bool:
        """Search every saved map for the latest scan (robot_nav.global_search)
        and act on it. While searching, a clear winner is seeded on its second
        clear win in a row (a scan taken while the robot is carried never
        repeats), loading its map first if it is on another one. While the pose is held, a rival (a clearly better place) named by
        two searches in a row stands, and is seeded at once if it is also the
        clear winner. Returns True to search again straight away: while
        searching, after a scan taken mid-turn, and after a rival's first
        naming, so the next search says whether it stands."""
        scan = self.latest_scan
        began, map_name = time.monotonic(), self.map_name
        if self.mode != "localize" or self.activity is not None or self.handled is not None or not self.scanning():
            return self.pose_state == "searching"
        stamp = Time.from_msg(scan.header.stamp)
        odom_laser = self.lookup_pose("odom", scan.header.frame_id, stamp)
        before = self.lookup_pose("odom", scan.header.frame_id, stamp - Duration(seconds=0.5))
        if odom_laser is None or before is None or abs(normalize_angle(odom_laser["yaw"] - before["yaw"])) > MAX_TURN_RADIANS:
            return True
        maps = robot_nav.saved_maps(self.args.maps_dir) if self.settings.other_maps else [self.map_name]
        candidates = []
        for name in maps:
            grids = self.search_grids(name)
            if grids is not None:
                found = robot_nav.global_search(grids, scan.ranges, scan.angle_min, scan.angle_increment, scan.range_min, scan.range_max, self.settings)
                candidates += [{**candidate, "map": name} for candidate in found]
        candidates.sort(key=lambda candidate: candidate["agree"], reverse=True)
        winner = robot_nav.clear_winner(candidates, self.settings)
        # where the last search's named place is now, carried by the odometry since
        named = None
        if self.named is not None:
            moved = robot_nav.relative_pose(self.named["odom_laser"], odom_laser)
            named = {**robot_nav.compose_poses(self.named["pose"], moved), "map": self.named["pose"]["map"]}

        seed = None
        with self.pose_lock:
            if self.handled_at > began or self.mode != "localize" or self.map_name != map_name:
                # picked up, or switched map or mode, while this search ran
                return False
            if self.pose_state == "searching":
                rival = None
                self.rival_stands = False
                if winner is not None and named is not None and robot_nav.same_place(named, winner):
                    seed = winner
                now_named = winner
            else:
                rival = self.find_rival(candidates, scan, odom_laser) if self.settings.rival else None
                self.rival_stands = rival is not None and named is not None and robot_nav.same_place(named, rival)
                if self.rival_stands:
                    log.info("a clearly better place stands on %s (%.2f, %.2f), agree %.2f", rival["map"], rival["x"], rival["y"], rival["agree"])
                    if winner is not None and robot_nav.same_place(winner, rival):
                        seed = winner
                now_named = rival
            self.named = None if now_named is None or seed is not None else {"pose": now_named, "odom_laser": odom_laser}
            self.search_summary = {
                "seconds": round(time.monotonic() - began, 2),
                "best": None if not candidates else {"map": candidates[0]["map"], "agree": round(candidates[0]["agree"], 2)},
                "winner": winner is not None,
                "rival": None if rival is None else ("stands" if self.rival_stands else "named"),
            }
            if seed is not None and seed["map"] == self.map_name:
                log.info("search: found the robot on %s (%.2f, %.2f), agree %.2f", seed["map"], seed["x"], seed["y"], seed["agree"])
                self.seed_from_search(seed, scan)
        if seed is not None and seed["map"] != self.map_name:
            self.switch_map(seed, scan)
        return self.pose_state == "searching" or (self.named is not None and not self.rival_stands)

    def search_grids(self, name: str) -> dict | None:
        """The search's grids for a saved map, built once and again when the
        map file changes. None when the map cannot be read."""
        yaml_path = Path(robot_nav.map_yaml_path(name, self.args.maps_dir))
        try:
            changed = yaml_path.stat().st_mtime
            cached = self.search_grids_cache.get(name)
            if cached is None or cached[0] != changed:
                data, map_info = robot_nav.load_map_file(name, self.args.maps_dir)
                self.search_grids_cache[name] = (changed, robot_nav.search_grids(data, map_info))
        except (OSError, KeyError, ValueError) as exc:
            log.warning("search: cannot read map %s: %s", name, exc)
            return None
        return self.search_grids_cache[name][1]

    def find_rival(self, candidates: list[dict], scan: LaserScan, odom_laser: dict) -> dict | None:
        """The rival to the held pose (robot_nav.find_rival), judged the way
        the tick judges it but on the saved map the search scores against."""
        map_odom = self.lookup_pose("map", "odom", Time())
        grids = self.search_grids(self.map_name)
        if map_odom is None or grids is None:
            return None
        held = {**robot_nav.compose_poses(map_odom, odom_laser), "map": self.map_name}
        check = robot_nav.beam_check(grids["data"], grids["map_info"], held, scan.ranges, scan.angle_min, scan.angle_increment, scan.range_min, scan.range_max, self.settings)
        if check is None:
            return None
        return robot_nav.find_rival(candidates, held, check["agree"], self.settings)

    def seed_from_search(self, laser_pose: dict, scan: LaserScan) -> None:
        """Hand AMCL a search's find: the lidar's map pose at the scan's time,
        turned into base_link's (the lidar sits off the axle) and carried to now."""
        base_laser = self.lookup_pose("base_link", scan.header.frame_id, Time())
        laser_base = robot_nav.relative_pose(base_laser, {"x": 0.0, "y": 0.0, "yaw": 0.0})
        self.set_initial_pose(robot_nav.compose_poses(laser_pose, laser_base), Time.from_msg(scan.header.stamp))

    def switch_map(self, laser_pose: dict, scan: LaserScan) -> None:
        """Load the map the search found the robot on (another floor) and seed
        it there. The tick's verdicts wait (activity) until AMCL has the seed:
        a map load leaves AMCL's particles where they were, which on the new
        map is simply a wrong pose."""
        if not self.busy.acquire(blocking=False):
            return
        self.activity = "switching map"
        try:
            name = laser_pose["map"]
            log.info("search: found the robot on %s twice, switching to it", name)
            rendered = self.map_rendered
            if not self.load_map(name):
                return
            self.map_name = name
            robot_nav.save_nav_config("localize", name, self.args.nav_config)
            self.update_keepout()
            # the tick judges against the grid it rendered, so wait for the new one
            deadline = time.monotonic() + SERVICE_WAIT_SECONDS
            while self.map_rendered is rendered and time.monotonic() < deadline:
                time.sleep(0.1)
            with self.pose_lock:
                self.seed_from_search(laser_pose, scan)
        finally:
            self.activity = None
            self.busy.release()

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
            # search now: forget the pose and look from scratch
            log.info("localize asked for: searching")
            with self.pose_lock:
                self.pose_state = "searching"
                self.clean = False
                Path(self.args.nav_pose).unlink(missing_ok=True)
                self.remembered_pose_written = None
            self.search_now.set()
            return {"ok": True, "accepted": True}
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
        if cmd == "record":
            return self.start_recording(request.get("label"), request.get("seconds", RECORD_SECONDS))
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
        self.pose_state = "searching"
        self.clean = False
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
        if self.state() in ("searching", "stale") or self.pose is None:
            return {"ok": False, "error": "not_localized"}
        if self.state() != "tracking":
            return {"ok": False, "error": "not_tracking", "note": "drive me a little first"}
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

    # --- recordings ------------------------------------------------------------

    def start_recording(self, label, seconds) -> dict:
        """Save the next `seconds` of scans with the poses the judge would use,
        one JSON line each, for scripts/localization-replay.py. The first line
        says what was recorded: the label (Arlen's sentence, or a taped cross),
        the map and the settings."""
        if not isinstance(label, str) or not label.strip():
            return {"ok": False, "error": "bad_label"}
        if not isinstance(seconds, (int, float)) or not 0 < seconds <= RECORD_MAX_SECONDS:
            return {"ok": False, "error": "bad_seconds"}
        if self.recording is not None:
            return {"ok": True, "accepted": False, "reason": "recording"}
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40]
        path = Path(self.args.recordings_dir) / f"{time.strftime('%Y-%m-%d-%H%M%S')}-{slug}.jsonl"
        header = {"label": label.strip(), "map": self.map_name, "mode": self.mode, "started": time.time(), "seconds": seconds, "settings": self.settings.to_dict()}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            recording_file = path.open("w")
            recording_file.write(json.dumps(header) + "\n")
        except OSError as exc:
            log.warning("could not start recording %s: %s", path, exc)
            return {"ok": False, "error": "recording_failed"}
        log.info("recording %.0fs: %s", seconds, label)
        self.recording = {"label": header["label"], "file": recording_file, "path": path, "until": time.monotonic() + seconds, "scans": []}
        return {"ok": True, "accepted": True, "path": str(path)}

    def write_recording(self) -> None:
        """Write the scans that came in since the last tick (the odometry for
        their time has arrived by now), and close the file once time is up."""
        recording = self.recording
        if recording is None:
            return
        scans, recording["scans"] = recording["scans"], []
        try:
            for scan, map_odom, amcl_pose, state in scans:
                stamp = Time.from_msg(scan.header.stamp)
                line = {
                    "time": stamp.nanoseconds / 1e9,
                    "scan_time": scan.scan_time,
                    "angle_min": scan.angle_min,
                    "angle_increment": scan.angle_increment,
                    "range_min": scan.range_min,
                    "range_max": scan.range_max,
                    "ranges": [round(value, 3) if math.isfinite(value) else None for value in scan.ranges],
                    # latest at the scan's arrival, the way the judge reads it
                    "map_odom": map_odom,
                    "odom_laser": self.lookup_pose("odom", scan.header.frame_id, stamp),
                    "odom_base": self.lookup_pose("odom", "base_link", stamp),
                    "map_laser": self.lookup_pose("map", scan.header.frame_id, stamp),
                    "amcl": amcl_pose,
                    "state": state,
                }
                recording["file"].write(json.dumps(line) + "\n")
        except OSError as exc:
            log.warning("recording %s failed: %s", recording["path"], exc)
            recording["until"] = 0.0
        if time.monotonic() >= recording["until"]:
            recording["file"].close()
            self.recording = None
            log.info("recorded %s", recording["path"])

    # --- navigation goals ----------------------------------------------------

    def navigating(self) -> bool:
        return self.goal is not None and self.goal["state"] == "navigating"

    def cancel_orphan_trip(self) -> None:
        """A bridge that died or restarted mid-trip left Nav2 driving with
        nothing watching it. An empty CancelGoal (zero goal id and stamp) asks
        Nav2 to cancel every goal it still has; once, when Nav2 first answers."""
        self.orphan_trip_checked = True
        self.nav_cancel_client.call_async(CancelGoal.Request()).add_done_callback(self.on_orphan_trip_cancelled)

    def on_orphan_trip_cancelled(self, future) -> None:
        if future.result().goals_canceling:
            log.warning("cancelled a trip Nav2 was still driving from before this bridge started")

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
        # Nav2 follows the lidar, so tick() sends the goal once both are up
        # and the search has found the robot if it did not know where it was
        self.goal_request = (self.goal_serial, self.map_name, float(x), float(y), yaw, time.monotonic())
        log.info("navigating to %s (%.2f, %.2f)", place_name or "a point", x, y)
        return {"ok": True, "accepted": True, "goal": self.goal}

    def send_pending_goal(self) -> None:
        if self.goal_request is None:
            return
        serial, map_name, x, y, yaw, asked_at = self.goal_request
        if self.activity is not None:
            # a map switch (or save) is running; the start clock runs from its end
            self.goal_request = (serial, map_name, x, y, yaw, time.monotonic())
            return
        if map_name != self.map_name:
            # the search found us on another map; the goal's x, y mean nothing here
            self.goal_request = None
            self.end_goal(serial, "failed", "place_on_other_map")
            return
        # searching, stale or not yet clean: wait for the search, the lidar and a passing verdict
        waiting_for_pose = self.mode == "localize" and not self.localized()
        if time.monotonic() - asked_at > GOAL_START_SECONDS:
            self.goal_request = None
            if not self.scanning():
                self.end_goal(serial, "failed", "lidar_unavailable")
            elif waiting_for_pose:
                self.end_goal(serial, "failed", "lost")
            else:
                self.end_goal(serial, "failed", "nav2_unavailable")
            return
        if not self.scanning() or waiting_for_pose or self.nav2_state != "active" or not self.nav_client.server_is_ready():
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
    parser.add_argument("--nav-config", default=robot_nav.DEFAULT_NAV_CONFIG_PATH)
    parser.add_argument("--places", default=robot_nav.DEFAULT_PLACES_PATH)
    parser.add_argument("--maps-dir", default=robot_nav.DEFAULT_MAPS_DIR)
    parser.add_argument("--barriers", default=robot_nav.DEFAULT_BARRIERS_PATH)
    parser.add_argument("--keepout-dir", default=robot_nav.DEFAULT_KEEPOUT_DIR)
    parser.add_argument("--map-png", default=DEFAULT_MAP_PNG_PATH)
    parser.add_argument("--nav-pose", default=DEFAULT_NAV_POSE_PATH)
    parser.add_argument("--localization-settings", default=DEFAULT_LOCALIZATION_SETTINGS_PATH)
    parser.add_argument("--sensors-config", default=DEFAULT_SENSORS_CONFIG_PATH)
    parser.add_argument("--recordings-dir", default=robot_nav.DEFAULT_RECORDINGS_DIR)
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
