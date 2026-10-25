# Lidar Safety, Map Persistence and Nav2

Goal: the lidar is part of the robot instead of a sensor that spins next to it.
It runs only when the robot can move, it stops the robot before it hits things
the ToFs cannot see, the house map survives a reboot, the robot works out where
it is on that map by itself, and both the dashboard and the voice assistant can
send it to a named place. Voice motion intents can stop or redirect a nav goal.

Picks up where [2026-10-12 - lidar.md](2026-10-12%20-%20lidar.md) stopped. The
drivetrain is calibrated (`WHEEL_DIAMETER_METERS` 0.1002, `TRACK_WIDTH_METERS`
0.320, both 2026-10-25), which was the precondition for everything below.

Work happens on the `ros2-nav2` branch. For this plan Claude may commit, push,
redeploy and restart; Arlen drives the robot for the on-floor checks and makes
the decisions marked **Arlen**.

## Decision record

| Decision | Choice |
|---|---|
| When the lidar spins | Only while the motor rail is on, or while something asks for it (relocalization at boot, a nav goal). The dashboard button stays as a manual override until the next rail edge. |
| Lidar in the safety gate | A min-range rule in a forward and a rear cone, same gate as the ToF rules. A stale or deactivated lidar disables the rule; it never blocks on its own absence, because the lidar is off most of the time by design. |
| Mapping vs localizing | Two modes, never both. `slam_toolbox` only while building a map. Day to day the robot runs `nav2_map_server` + AMCL against a saved map. |
| How many maps | One map per floor, one floor to start. "Which map am I on" is a scan-match score across the saved maps; built only if the robot ever lives on more than one floor (**Arlen**). |
| Where the robot starts | Anywhere. On boot it runs AMCL global localization and spins in place until the pose estimate converges. No dock requirement. |
| Nav2 controller | Regulated Pure Pursuit. Light enough for the Pi 5 and made for differential drive. |
| Voice vs nav arbitration | Voice intents win. The motion node holds a `twist_mux` lock while an intent runs, which mutes `/cmd_vel_nav`, and the nav bridge cancels the active goal when it sees an intent start. A new voice goal preempts the old one. |
| Named places | `~/.config/robot-pet/places.json`: `{name: {map, x, y, yaw}}`. Set from the dashboard ("save place here") or by voice ("remember this spot as the kitchen"). |

## Phase 0 - Sensors node watchdog (small, unrelated, do first)

The I2C bus hang on 2026-10-25 left `robot-sensors` alive but stuck in the
kernel, so systemd never restarted it. A tick that does not return within a
few seconds exits the process; systemd's `Restart=` brings it back.

Verify: kill a sensor read mid-flight on the robot (`ssh`, `kill -STOP` the
thread is not practical; instead unplug the ToF daisy chain for ten seconds)
and watch the unit restart in `journalctl -u robot-sensors`.

## Phase 1 - Lidar follows the motor rail

- `src/ros_nodes/lidar_status_node.py` already subscribes to the hub and owns
  the lifecycle client. Add: on `motor_rail.state` going on, `activate`; on
  going off, `deactivate`. A `lidar_requested` set in the hub (from Phase 3's
  relocalization and Phase 4's nav bridge) also keeps it on.
- Dashboard `/lidar/power` becomes an override that holds until the next rail
  edge, so "turn it on to look at the scan" still works without a gamepad.
- `docs/ARCHITECTURE.md` lidar row and the dashboard panel text.

Verify (Arlen): connect the gamepad, lidar starts within a second or two.
Disconnect, it stops a few seconds after the rail drops. Dashboard button
turns it on with no gamepad; connecting the gamepad takes over.

## Phase 2 - Lidar in the safety gate

- `motion_node.py` subscribes `/scan` and hands the motion loop the min range
  in two cones: forward (±25°) and rear (±25°), as distances from the laser.
- `robot_model.py`: the lidar sits 82 mm behind the axle, the bumper 55 mm
  ahead, the caster 266 mm behind. The rule converts laser range to "distance
  past the body edge" so the threshold is one number in `sensors.json`
  (`lidar_stop_distance_m`, start at 0.15).
- `safety_gate.py`: `lidar_forward_obstacle` blocks forward wheel speed,
  `lidar_rear_obstacle` blocks reverse. Turning in place is never blocked by
  the lidar. Scan older than 1 s, or lidar not active, means the rule is off.
- Nav2 will also stop for obstacles through its costmap; the gate is the
  backstop for the gamepad, voice moves and a misbehaving planner.

Verify (Arlen): box at knee height 1 m ahead, drive at it with the gamepad.
Stops with about 15 cm to spare. Same backwards. A box below the scan plane
(under 20 cm) is still caught by the forward ToFs as before.

## Phase 3 - Map persistence and relocalization

- `setup.sh`: `ros-lyrical-nav2-map-server ros-lyrical-nav2-amcl`
  (and `ros-lyrical-navigation2 ros-lyrical-nav2-bringup` for Phase 4).
- Maps live in `~/.config/robot-pet/maps/<name>.yaml` + `.pgm`. Save with
  `nav2_map_server`'s `map_saver_cli` from the mapping session.
- New node `src/ros_nodes/nav_node.py` ("nav bridge"), one process:
  - owns the mode: `mapping` (starts `robot-slam.service`) or `localize`
    (starts `robot-localization.service`: map_server + AMCL with the chosen
    map). Mode and map name persist in `~/.config/robot-pet/nav.json`.
  - on boot in `localize` mode: asks for the lidar, calls AMCL
    `reinitialize_global_localization`, sends a `turn 360` intent, waits for
    the pose covariance to drop below a threshold, repeats once, then
    releases the lidar. Publishes `localized: true/false` to the hub.
  - publishes the map-frame pose to the hub every 0.5 s for the dashboard.
  - saves and lists places (`places.json`).
- Dashboard: Map panel shows `/map` from Foxglove-style occupancy rendering
  with the robot pose and the saved places. Buttons: Start mapping, Save map
  as..., Localize, Save place here. Follow the voice-toggle button pattern.
- `twist_mux.yaml`: add the `nav_lock` topic now so Phase 4 is config-free.

Verify (Arlen): map one room plus hallway with the gamepad, save as `home`.
Reboot. Robot spins, dashboard shows it localized at the right spot. Carry it
to another room, reboot, same result. Save two places.

## Phase 4 - Nav2

- `config/nav2.yaml`: footprint from `robot_model.py`, local and global
  costmaps from `/scan`, NavFn planner, Regulated Pure Pursuit controller,
  spin and backup behaviors, BT navigator. Velocity limits well below what
  the gamepad allows. Publishes `/cmd_vel_nav`.
- `systemd/robot-nav2.service`: `nav2_bringup navigation_launch.py` with our
  params, `After=robot-localization.service`.
- Nav bridge grows: `navigate_to(place | x,y,yaw)` and `cancel` over a unix
  socket (`/run/robot-pet/nav.sock`), NavigateToPose action client, requests
  motor power and the lidar while a goal is active, publishes goal status and
  the current path to the hub. Cancels the goal when the hub shows a motion
  intent starting.
- Motion node: publishes `std_msgs/Bool` on `/nav_lock` (true while an intent
  runs) so `twist_mux` mutes nav during voice moves.
- Voice tools (`src/voice/tools.py`): `go_to(place)`, `stop_navigation`,
  `remember_place(name)`, `where_am_i`. `go_to` returns immediately with
  "on my way"; the assistant is told on arrival or failure through the same
  event path face_me uses. Any existing motion tool cancels the goal first.
- Dashboard: click a point on the map to send a goal, Cancel button, status
  line (navigating to kitchen, 2.1 m left / arrived / failed: path blocked).

Verify (Arlen): click a point two rooms away, robot gets there. Say "stop"
mid-route, it stops within a second and the dashboard says cancelled. Say
"go to the kitchen" mid-route, it redirects. Put a chair in the path, it
plans around. Gamepad input mid-route stops nav for good.

## Exit criterion

Cold boot with no gamepad: lidar spins briefly, robot finds itself on the
saved map, lidar stops. "Hey Bloop, go to the kitchen": lidar starts, robot
drives there avoiding furniture, says it has arrived, lidar stops. Drive it
at a box with the gamepad: it refuses. `./setup.sh` on a fresh card installs
everything this plan added.

## Risks

| Risk | Mitigation |
|---|---|
| AMCL global localization converges to the wrong symmetric spot | Two spins instead of one; show confidence on the dashboard; a "Localize" button to retry. If a room is truly ambiguous, a short forward move during localization breaks it. |
| Lidar at 203 mm misses low furniture rails and the robot hits them under Nav2 | Keep the ToF rules; add a Nav2 `voxel`/range layer from the forward ToFs if it bites. |
| Pi 5 CPU with AMCL + costmaps + RPP | Measure `top` in Phase 4 like the slam phase did. Lower costmap update rates first. |
| Odometry quality under Nav2 | Calibrated 2026-10-25 to 0.1° per 218° turn and 2.00 m per 2.00 m. Good. |
| Rail off kills odometry mid-goal | Nav bridge requests motor power for the life of the goal; the existing hold covers the gaps. |
| Lifecycle flapping on the lidar if the rail bounces | Debounce the rail edge by a second in Phase 1. |

## Open for Arlen

1. One floor, or does the robot get carried between floors? Decides whether
   "which map" is built at all.
2. Place names to start with, so the voice prompt can mention them.
