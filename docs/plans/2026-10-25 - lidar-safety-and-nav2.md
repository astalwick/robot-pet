# Lidar Safety, Map Persistence and Nav2

Goal: the lidar is part of the robot instead of a sensor that spins next to it.
It runs only when the robot can move, it stops the robot before it hits things
the ToFs cannot see, the house maps survive a reboot, the robot works out which
floor it is on and where, and both the dashboard and the voice assistant can
send it to a named place. Voice motion intents stop or redirect a nav goal.

Picks up where [2026-10-12 - lidar.md](2026-10-12%20-%20lidar.md) stopped. The
drivetrain is calibrated (`WHEEL_DIAMETER_METERS` 0.1002, `TRACK_WIDTH_METERS`
0.320, both 2026-10-25), which was the precondition for everything below.

Work happens on the `ros2-nav2` branch. For this plan Claude may commit, push,
redeploy and restart; Arlen drives the robot for the on-floor checks. While the
drive rail is switched off at the wall, nothing here can be driven, so the
on-robot checks Claude can do are: units start, topics and services exist,
lifecycle transitions take, tests pass. Floor checks are listed per phase for
Arlen.

Answers already in hand: the robot lives on **several floors** (so "which map"
is built, not deferred); first places are **kitchen** and **living room**.

## Review against the codebase (2026-10-25, before starting)

The first draft was written from memory of the code. Reading it back against
the actual code and the two earlier plans changed the shape in these places:

| Draft said | Actually | Change |
|---|---|---|
| Phase 0: a tick watchdog exits the sensors process when a read hangs. | The 2026-10-25 hang was a thread stuck in the kernel (`i2c_dw_xfer_common`, uninterruptible). Neither `os._exit` nor systemd's SIGKILL can reap a thread in that state; the process only dies when the I2C transfer returns. The dashboard already shows the `sensors` source stale. | **Phase 0 dropped.** A userland watchdog would be code that cannot do its job. If the hang recurs the fix is on the bus (mux reset line, clock stretching), not in Python. |
| `lidar_status_node.py` "already subscribes to the hub". | It only publishes to the hub. | Phase 1 adds a hub subscriber thread. |
| A `lidar_requested` hub flag from relocalization and nav keeps the lidar on. | The motion runner already sets `drive_status.motion_power_requested` for any intent and any non-zero `/cmd_vel`, and `robot_battery` turns the rail on for it. Relocalization spins with a `turn` intent; Nav2 drives through `/cmd_vel`. | One rule only: **the lidar follows the motor rail**. The nav bridge asks for the rail (one field in its own hub source, read by `robot_battery` the same way it reads `motion_power_requested`) and the lidar follows. No second flag. |
| Dashboard override detection in the node. | The dashboard already drives the lifecycle directly with `ros2 lifecycle set`. If the node acts only on rail *edges* (not continuously), a manual change simply holds until the next edge, with no override logic at all. | Edge-triggered node; dashboard code unchanged. `rplidar.launch.py` stops auto-activating so the lidar is off at boot until the rail comes on. |
| Lidar rule is a ±25° cone, off when "scan stale or lidar not active". | The runner cannot see lifecycle state, and does not need to: a deactivated lidar publishes no scans, so staleness covers it. A cone lets a point 0.4 m to the side at 1 m range count as "ahead". | Corridor, not cone: points within the body's width plus a margin, measured as distance past the front (or rear) edge using `robot_model.py`. Staleness only. |
| `safety_gate.py` grows two reasons and "blocks reverse". | `SafetyState` is one `blocked` flag and `cancel_forward_qpps_when_blocked` only ever cancels forward. The ToF rule *blocks everything while the ToFs are stale*; the lidar rule must not. | `SafetyState` gains `reverse_blocked`; the cancel function handles both directions. The ToF stale rule is untouched. |
| Phase 3 adds `nav2-map-server`, `nav2-amcl`, `navigation2` to `setup.sh`. | `ros-lyrical-nav2-bringup` is already in `setup.sh` and pulls the whole stack. Confirmed on the robot: amcl, map_server, bt_navigator, behaviors, navfn, smac, regulated_pure_pursuit, slam_toolbox 2.10, twist_mux all installed. | No package work. |
| `robot-slam.service` and a new `robot-localization.service`, started and stopped by the nav bridge. | Every unit in this repo is enabled at boot and listed in `setup.sh`, `restart.sh`, `redeploy-robot.sh` and its test. On-demand units would be a new pattern, and `redeploy` would restart the wrong one. | **One unit, `robot-nav.service`**, runs `config/nav.launch.py`, which reads `nav.json` and launches either slam_toolbox or map_server + AMCL (Phase 4 adds Nav2 to both). Mode switch = write `nav.json`, `sudo systemctl restart robot-nav.service`, the same way the dashboard already restarts units. `robot-slam.service` is retired. |
| Nav bridge verbs `navigate_to`/`cancel`; voice tools `go_to`, `stop_navigation`, `remember_place`, `where_am_i`. | [2026-07-13 - async-nav-goals.md](done/2026-07-13%20-%20async-nav-goals.md) already decided the socket verbs (`navigate_to`, `nav_status`, `nav_cancel`, `create_place`) and that the goal runner polls. | Use those names. Voice tools: `navigate_to`, `check_navigation`, `cancel_navigation`, `create_place`, `where_am_i`. |
| Arrival is announced "through the same event path face_me uses". | `face_me` is a blocking call. The voice command socket only knows `talk_now`, `end_session`, `set_personality`; nothing can make the robot speak unprompted. | `navigate_to` returns at once. Inside a `start_goal` the runner polls `check_navigation`; otherwise the user asks. An unprompted "I'm here" needs a new voice command and is a follow-up, not this plan. |
| Nav bridge "cancels when it sees an intent start" in the hub. | `drive_status` has `state` and `stop_reason` but nothing that names a running intent. | `drive_status` gains `intent` (tool name or null). The bridge also subscribes `/cmd_vel_teleop`: any non-zero twist cancels the goal for good. |
| `nav_lock` added to `twist_mux.yaml` in Phase 3 "so Phase 4 is config-free". | Nothing publishes it until Phase 4. | Added in Phase 4 with its publisher. |
| One floor, "which map" maybe later. | Several floors. | Phase 3 tries the saved maps in turn, last used first, and keeps the one AMCL converges on. |

## Decision record

| Decision | Choice |
|---|---|
| When the lidar spins | Only while the motor rail is on. Anything that needs the lidar (relocalization, a nav goal) asks for the rail, and the lidar follows. The dashboard button is a manual override that holds until the rail next changes. |
| Lidar in the safety gate | A min-clearance rule in a forward and a rear corridor, in the same gate as the ToF rules. No scan for a second means the rule is off; it never blocks on its own absence, because the lidar is off most of the time by design. |
| Mapping vs localizing | Two modes, never both. `slam_toolbox` only while building a map. Day to day: `nav2_map_server` + AMCL against a saved map. One systemd unit, `robot-nav.service`, whose launch file picks from `nav.json`. |
| How many maps | One map per floor. On boot, try the saved maps in turn (last used first): load, global localization, one 360 spin, check the AMCL covariance. First map that converges wins. None converge: `localized: false`, dashboard offers Localize and a map picker. |
| Where the robot starts | Anywhere. No dock. |
| Nav2 controller | Regulated Pure Pursuit. Light enough for the Pi 5 and made for differential drive. |
| Voice vs nav arbitration | Intents win. The motion node publishes `/nav_lock` while an intent runs (and for 2 s after), which makes `twist_mux` drop `/cmd_vel_nav`; the nav bridge cancels the goal when `drive_status.intent` appears or `/cmd_vel_teleop` moves. A new `navigate_to` preempts the old goal. |
| Named places | `~/.config/robot-pet/places.json`: `{name: {map, x, y, yaw}}`. Set from the dashboard or by voice (`create_place`). |
| Hub shape | One new hub source, `nav`, owned by the nav bridge, listed in `robot_telemetry.build_snapshot` like `lidar` and `motor_rail`. No event bus, no schema registry (per the 2026-06-09 plan's "do not" list). |

## Phase 0 - dropped

See the review table. The I2C hang is a kernel-side wait; nothing in the
sensors process can shorten it.

## Phase 1 - Lidar follows the motor rail

- `config/rplidar.launch.py`: configure only, do not activate. The lidar is
  off at boot until something turns the rail on.
- `src/ros_nodes/lidar_status_node.py`: a thread iterates the hub
  `subscribe()` stream and records whether the rail is on (`motor_rail.state`
  is `on` or `warning`, source fresh). The 0.5 s timer acts on edges: rail
  came on, `activate`; rail has been off for 3 s, `deactivate` (the 3 s is the
  flap debounce). When the driver (re)appears, apply the current rail state
  once. A `ChangeState` client next to the existing `GetState` one.
- Dashboard: no code change. Panel text under the button says the lidar
  follows the motor rail and the button overrides until the rail next changes.
- `docs/ARCHITECTURE.md` lidar row.

Claude verifies: unit restarts clean, `ros2 lifecycle get /rplidar_node` says
`inactive` after a driver restart with the rail off, dashboard button still
activates and deactivates.

Arlen verifies: connect the gamepad, lidar starts within a couple of seconds.
Disconnect, it stops about 8 s later (5 s rail hold + 3 s debounce).
Dashboard button turns it on with no gamepad; it stays on until the gamepad
connects and disconnects again.

## Phase 2 - Lidar in the safety gate

- `motion_node.py` subscribes `/scan`. Each scan becomes two clearances,
  forward and rear, handed to the runner under the same lock as the ToF
  readings, with a receive time.
- `safety_gate.py`: `scan_clearances(ranges, angle_min, angle_increment,
  range_min, range_max)` converts polar points through `LIDAR_MOUNT` into
  base_link, keeps points inside the body's width (`FOOTPRINT_HALF_WIDTH` +
  3 cm margin), and returns the smallest distance past `FOOTPRINT_FRONT_X`
  and past `FOOTPRINT_REAR_X`. Points inside the footprint (the robot's own
  deck furniture) are ignored.
- `SafetyConfig.lidar_stop_below_mm` (default 150, in `sensors.json` under
  `safety`). `evaluate_safety` takes the clearances and their age; older than
  1 s, or `None`, means the rule is off. Forward clearance under the threshold
  sets `blocked` with reason `lidar_forward_obstacle`; rear sets
  `reverse_blocked` with `lidar_rear_obstacle`. Turning in place is never
  blocked. `cancel_forward_qpps_when_blocked` becomes `cancel_blocked_qpps`
  and cancels the reverse component too.
- Nav2 will also stop for obstacles through its costmap; the gate is the
  backstop for the gamepad, voice moves and a misbehaving planner.

Claude verifies: unit tests for the geometry (a point dead ahead at 1 m from
the laser is 0.854 m past the front edge; a point behind at 1 m is 0.816 m
past the rear edge, since the rear edge is 184 mm behind the laser; a point
0.5 m to the side is ignored), and the gate's stale, forward, rear and turning
cases. Done 2026-10-25.

Arlen verifies: box at knee height 1 m ahead, drive at it with the gamepad.
Stops with about 15 cm to spare. Same backwards. A box below the scan plane
(under 20 cm) is still caught by the forward ToFs as before. Note the robot's
`sensors.json` has `forward_stop_below_mm` at 250, so the ToFs will usually
fire first on things they can see.

## Phase 3 - Map persistence, floor detection, relocalization

### Phase 3 review (2026-10-25, before starting)

Read against Nav2 1.5.2 and slam_toolbox 2.10 as installed on the robot, and
against the hub, battery, dashboard and redeploy code:

| Draft said | Actually | Change |
|---|---|---|
| Mapping includes nav2_bringup `slam_launch.py`. | That launch runs slam_toolbox in *sync* mode, adds a `map_saver_server`, expects the nav2 params layout and defaults `use_sim_time` to true. Today's `robot-slam.service` runs slam_toolbox's own `online_async_launch.py`. | `nav.launch.py` includes `online_async_launch.py` with `slam_params_file` and `use_sim_time:=false`, the same thing robot-slam runs today. Saving a map is `map_saver_cli -f ~/.config/robot-pet/maps/<name>` run as a subprocess by the nav bridge (ros-node.sh has already sourced the ROS env). |
| Localize includes nav2_bringup `localization_launch.py`. | In Nav2 1.5 that file starts only `map_server` and `amcl`; the lifecycle manager lives in `bringup_launch.py`. Included alone, both nodes would sit unconfigured forever. | `nav.launch.py` starts the three nodes itself: `nav2_map_server/map_server`, `nav2_amcl/amcl`, and `nav2_lifecycle_manager/lifecycle_manager` (`autostart`, `node_names: [map_server, amcl]`). Phase 4 adds the navigation nodes the same way. |
| `config/nav2.yaml` "AMCL section". | Stock AMCL params say `base_frame_id: base_footprint` and `laser_max_range: 100`. Our tree has no `base_footprint`. | `base_frame_id: base_link`, `laser_max_range: 12.0` (the A1), `set_initial_pose: false`; otherwise stock. |
| Pose from `/amcl_pose`, "or slam's TF while mapping". | Two code paths for one number. In both modes `map -> base_link` is in TF. | Pose always from TF. `/amcl_pose` is read only for the covariance check (converged when x and y variance are under 0.25 m² and yaw variance under 0.2 rad²). |
| `nav_node.py` holds the logic. | ARCHITECTURE: loops and logic in `src/*.py`, testable without rclpy; the node is thin. | `src/robot_nav.py`: config files (`nav.json`, `places.json`, maps dir), map grid to PNG, pose to pixel, covariance check, the socket server, the hub message. `nav_node.py` only wires ROS to it. |
| `nav.json` absent on first deploy. | Nothing says what happens. | Missing `nav.json`, or a map file that is gone: mapping mode. First deploy behaves exactly like today's robot-slam. |
| `lidar_update.slam_active` stays. | It asks systemd about `robot-slam.service`, which is retired. | Becomes `nav_active` (`robot-nav.service`); the Lidar panel row and the two tests follow. |
| Sudoers rows needed for the new units. | `pi` has blanket passwordless sudo on this robot; `setup.sh` also writes a row per `ROBOT_SERVICES` entry for a fresh card. | Add the units to `ROBOT_SERVICES` and nothing else. |
| Relocalization blocks the node. | The `turn 360` intent is a blocking request (up to 40 s). | Runs on the bridge's own thread: request the rail, wait for fresh scans, then per map `load_map`, `reinitialize_global_localization`, `turn 360`, check covariance. The ROS executor keeps spinning. |
| Dashboard buttons "Save map as..." with a prompt. | Browser prompt dialogs block the page (and automation). | Inline text inputs next to the buttons. Mode buttons follow the voice-toggle pattern. |

- Maps: `~/.config/robot-pet/maps/<name>.yaml` + `.pgm`, written by
  `map_saver_cli`. `nav.json`: `{"mode": "mapping" | "localize", "map": name}`.
- `config/nav.launch.py` reads `nav.json`; `mapping` includes slam_toolbox's
  `online_async_launch.py` with `config/slam_toolbox.yaml`; `localize` starts
  `map_server` (the map yaml), `amcl` (`config/nav2.yaml`) and a lifecycle
  manager. `systemd/robot-nav.service` runs it. `robot-slam.service` is
  retired (unit deleted; lists updated in `setup.sh`, `restart.sh`,
  `redeploy-robot.sh`, its test, `ARCHITECTURE.md`).
- `src/robot_nav.py` (logic) + `src/ros_nodes/nav_node.py` ("nav bridge"),
  `systemd/robot-nav-bridge.service`:
  - owns `nav.json` and `places.json`; mode switch = write, then
    `sudo systemctl restart robot-nav.service`.
  - publishes hub source `nav` every 0.5 s: `mode`, `map`, `maps` (saved
    names), `localized`, `pose` (map frame x, y, yaw from TF), `map_info`
    (origin, resolution, size, and a serial that changes when the PNG does),
    `places` and `motor_power_requested`. `robot_telemetry.build_snapshot`
    lists it. `robot_battery` reads `nav.motor_power_requested` exactly like
    `drive_status.motion_power_requested`.
  - writes the current `/map` as `/run/robot-pet/map.png` whenever a new map
    message arrives (at most every 2 s) so the dashboard can show it without
    speaking ROS.
  - on boot in `localize` mode: sets `motor_power_requested` (rail on, lidar
    follows), then for each saved map, last used first: `load_map`,
    `reinitialize_global_localization`, `turn 360` intent, check the
    `/amcl_pose` covariance. Converged: remember the map, `localized: true`,
    release the rail. None: `localized: false`.
  - unix socket `/run/robot-pet/nav.sock`, verbs this phase: `nav_status`,
    `set_mode`, `save_map`, `localize`, `create_place`.
- Dashboard: Map panel. The PNG with the robot pose and places drawn over it,
  mode and map name, `localized`. Buttons following the voice-toggle pattern
  (DOM writes only on change, never disabled while busy): Start mapping, Save
  map as (text input), Localize with a map picker, Save place here (text
  input). The dashboard serves `/map.png` from `/run/robot-pet` and forwards
  `/nav/command` to the socket like `/voice/command`.

Claude verifies: `robot-nav.service` comes up in both modes with the rail off
(no scans, but map_server serves the map and AMCL waits); `nav` source
appears in the hub; `nav_status` over the socket; `set_mode` restarts the
unit; tests for `nav.json`/`places.json` handling, the PNG and pixel math,
the covariance check and the socket verbs. Done 2026-10-25.

Arlen verifies: map one floor with the gamepad, Save map as `main`. Reboot.
Robot spins once, dashboard shows it localized at the right spot. Carry it to
another room on the same floor, reboot, same result. Map the second floor as
`upstairs`. Carry it upstairs, reboot: it tries `main`, fails, tries
`upstairs`, converges. Save `kitchen` and `living room`.

## Phase 4 - Nav2

### Phase 4 review (2026-10-25, before starting)

Read against Nav2 1.5.2 as installed on the robot (`ros2 pkg list`, the stock
`navigation_launch.py` and `nav2_params.yaml`, twist_mux's lock semantics)
and against the motion runner, the intent bridge, the voice tool plumbing
and the dashboard:

| Draft said | Actually | Change |
|---|---|---|
| `nav.launch.py` includes nav2_bringup `navigation_launch.py`. | That launch starts `docking_server` and `following_server`, whose packages (`opennav_docking`, `opennav_following`) are not installed, plus `route_server`, `smoother_server`, `waypoint_follower`, `collision_monitor` and `velocity_smoother`, and like `localization_launch.py` it has no lifecycle manager. | Same shape as Phase 3: `nav.launch.py` starts `controller_server`, `planner_server`, `behavior_server`, `bt_navigator` and a `lifecycle_manager_navigation` itself, in both modes, with `cmd_vel` remapped to `cmd_vel_nav` on the controller and behavior servers. |
| Velocity smoother. | The motion runner already slews every `/cmd_vel` centrally (`control/`), and the safety gate already stops for lidar and ToF. A smoother and a collision monitor would be two more processes on a 4-core Pi doing what the motion node does. | Neither is launched. |
| Nav2 publishes `/cmd_vel_nav`. | Nav2 1.5's controller and behavior servers publish `TwistStamped` by default; twist_mux runs with `use_stamped: false`. | `enable_stamped_cmd_vel: false` on `controller_server` and `behavior_server`. |
| Footprint "from `robot_model.py`". | `nav2.yaml` is static YAML read by Nav2, not by our Python. Generating it or importing `robot_model` in the launch file is machinery for four numbers. | The polygon is written into `nav2.yaml` by hand with a comment, and a test asserts the YAML text carries the polygon built from `robot_model`'s constants. |
| "Limits well below the gamepad's." | Gamepad normal speed is `DEFAULT_QPPS` × `speed_scale` ≈ 0.36 m/s; there is no named max-speed constant to point at. | RPP `desired_linear_vel: 0.25`, rotate-to-heading 0.8 rad/s; behaviors `max_rotational_vel: 0.8`; local costmap 3 Hz, controller 10 Hz, planner on demand. |
| `/nav_lock` "priority between nav and teleop". | twist_mux locks are `std_msgs/Bool`; a lock blocks every topic whose priority is below the lock's, and with `timeout > 0` a lock whose publisher goes quiet engages. | `locks: nav_lock: {topic: /nav_lock, timeout: 0.5, priority: 50}`: mutes nav (10), never teleop (100); a dead motion node mutes nav too, which is right. |
| Lock true "while an intent runs and for 2 s after". | A voice `stop` is not an intent, it cancels one. If Nav2 is mid-goal when the user says stop, nothing holds nav muted while the bridge cancels. | The runner keeps `nav_lock_until`: set 2 s past now whenever an intent ends or a stop is serviced. Lock = intent active, or now < `nav_lock_until`. |
| `drive_status["intent"]` for the bridge to watch. | The bridge would need a hub subscriber thread for one boolean that the motion node already publishes as `/nav_lock`. | The bridge subscribes `/nav_lock` and cancels its goal when it goes true. No `drive_status.intent`, no hub subscriber. |
| Voice `stop` goes to the motion node only. | The 2026-07-13 plan already fixed that `stop` grows a nav cancel. Through the lock alone the cancel is indirect. | `stop` sends the motion stop, then `nav_cancel` over the nav socket. |
| "The goal runner polls `check_navigation`." | Each poll is a model round trip (seconds and tokens) for a goal that takes a minute. | `check_navigation(wait_seconds)` waits up to that long for the goal to finish before answering, polling the socket twice a second. The tool description says to use it. |
| `navigate_to(place \| x, y, yaw)`. | A dashboard click has no yaw. Places have one. | Yaw optional over the socket: missing means face the target from where the robot stands. A place goal needs localize mode and `localized`; an x, y goal also works while mapping (slam gives the pose). |
| `where_am_i` over the socket. | The hub `nav` source already carries map, pose and places; `check_health` already reads the hub snapshot through `robot_inspection_caller`. | Pure function over the snapshot, no socket call. |
| Nav bridge "cancels the goal when `/cmd_vel_teleop` moves". | Right, and a mode switch or a relocalize must do the same: `switch_to` restarts the Nav2 action server under the goal, and relocalize spins the robot. | `navigate_to` is refused while the bridge is `busy`; `switch_to` marks a running goal cancelled and drops the rail request. |
| Redirect mid-route. | A second `NavigateToPose` goal preempts the first inside bt_navigator; the first's result arrives late as cancelled. | Goals carry a serial; a result for an old serial is ignored. |

- `config/nav2.yaml` grows `bt_navigator`, `controller_server` (RPP,
  SimpleProgressChecker, SimpleGoalChecker), `local_costmap` (rolling 3 m,
  obstacle + inflation layers from `/scan`), `global_costmap` (static +
  obstacle + inflation), `planner_server` (NavFn), `behavior_server` (spin,
  backup, wait). Footprint polygon from `robot_model.py`'s numbers, frames
  `map`/`odom`/`base_link`, `enable_stamped_cmd_vel: false`. Limits as in the
  table. `nav.launch.py` starts those four nodes and
  `lifecycle_manager_navigation` in both modes.
- `twist_mux.yaml`: lock `/nav_lock` (timeout 0.5, priority 50).
  `motion_node.py` publishes `std_msgs/Bool /nav_lock` on its 20 Hz timer from
  `runner.nav_lock_active()`: true while an intent runs and for 2 s after an
  intent ends or a stop is serviced.
- Nav bridge grows: `navigate_to(place | x, y[, yaw])` and `nav_cancel` over
  the socket, a `NavigateToPose` action client, `motor_power_requested` for
  the life of the goal, `goal` in the `nav` hub message: `target` (place name
  or null), `x`, `y`, `state` (`navigating`, `arrived`, `cancelled`,
  `failed`), `remaining_m`. Cancels the goal when `/nav_lock` goes true or
  `/cmd_vel_teleop` moves. Refused while `busy`; `switch_to` cancels.
- Voice tools (`src/voice/tools.py`): `navigate_to(place)` (returns at once;
  unknown place returns the list of known places), `check_navigation
  (wait_seconds)`, `cancel_navigation`, `create_place(name)`, `where_am_i`
  (nearest place on the current map within 2 m, else map name and
  coordinates). `stop` also sends `nav_cancel`. A `nav_caller` joins the
  other callers in `VoiceToolContext`, wired from `robot_voice.py` through
  the session, the orchestrator and the goal runner like `face_me_caller`.
- Dashboard: `NAV_COMMANDS` gains `navigate_to` and `nav_cancel`; click the
  map to send an x, y goal, Cancel button, `goal` row (navigating to kitchen,
  2.1 m left / arrived / failed / cancelled).
- `docs/ARCHITECTURE.md`: the two known-future items become the description
  of this arbitration.

Claude verifies: Nav2 lifecycle comes up in both modes, `/navigate_to_pose`
action exists, `navigate_to` with the rail off ends as a failed goal (not a
hang), `/nav_lock` goes true on a stop intent and twist_mux drops
`/cmd_vel_nav`, `top` for the whole stack.

Arlen verifies: click a point two rooms away, robot gets there. Say "stop"
mid-route, it stops within a second and the dashboard says cancelled. Say "go
to the kitchen" mid-route, it redirects. Put a chair in the path, it plans
around. Gamepad input mid-route stops nav for good.

## Exit criterion

Cold boot with no gamepad: lidar spins briefly, robot finds itself on the
right floor's map, lidar stops. "Hey Bloop, go to the kitchen": lidar starts,
robot drives there avoiding furniture, lidar stops. Drive it at a box with
the gamepad: it refuses. `./setup.sh` on a fresh card installs everything
this plan added.

## Risks

| Risk | Mitigation |
|---|---|
| AMCL converges on the wrong map or the wrong symmetric spot | Last-used map first; dashboard shows `localized` and a Localize button with a map picker. If a room is truly ambiguous, a short forward move during localization breaks it. |
| Lidar at 203 mm misses low furniture rails and the robot hits them under Nav2 | Keep the ToF rules; add a Nav2 range layer from the forward ToFs if it bites. |
| Pi 5 CPU with AMCL + costmaps + RPP | Measure `top` after Phase 3 and Phase 4. Lower costmap update rates first. |
| Rail off kills odometry mid-goal | The nav bridge holds `motor_power_requested` for the life of the goal. |
| Lifecycle flapping on the lidar if the rail bounces | 3 s off-debounce in Phase 1. |
| `sudo systemctl restart` from the nav bridge | Same path the dashboard already uses for gamepad-teleop, sensors and motion. |
