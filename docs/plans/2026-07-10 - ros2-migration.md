# ROS2 Migration Plan

Goal: take the robot from eleven plain systemd services to a working ROS2 (Lyrical Luth)
motion core — `/cmd_vel`, `/odom` + TF, `sensor_msgs/Range`, `BatteryState`,
Foxglove — with the web dashboard, voice, camera, and vision continuing to work
untouched. This is the migration the
[2026-07-10 ros2-migration-prep plan](2026-07-10%20-%20ros2-migration-prep.md)
prepared for. The lidar itself is **not** in this plan; the done criterion is
"drop in a lidar node and slam_toolbox next, nothing else has to move."

## Decision record (agreed 2026-07-10)

| Decision | Choice |
|---|---|
| OS / install | Reflash to **Ubuntu 26.04 + ROS2 Lyrical Luth** apt binaries. Spike on a **spare SD card** first; the current Trixie card is imaged and kept as rollback. |
| Scope | **Motion core only**: motion, gamepad teleop, sensors, odometry/TF, battery states become ROS. Voice, camera, vision, brain, web dashboard, telemetry hub stay plain services. |
| Dashboard | Keeps working unmodified. See "dual-publish" note below. |
| Layout | Minimal: plain node files in `src/ros_nodes/`, no colcon workspace yet (refinement of the "one ament package" decision — see below). |
| Supervision | Per-node systemd units, exactly like today. `ros2 launch` enters later only when slam/nav2 forces it. |
| Teleop | Port our `gamepad_teleop` (keep feel, tuning, telemetry); it publishes `Twist` via the prep-plan kinematics. |
| Testing | `python3 -m unittest` on the Mac stays green, forever. Node files are thin, import `rclpy`, and are excluded from off-robot tests. |
| Viz | `foxglove_bridge` from day one. |

**Amended 2026-07-13:** originally targeted Ubuntu 24.04 + Jazzy. The Phase 0
spike found 24.04 cannot drive the Pi 5 camera at all — its libcamera predates
PiSP and ships no `ipa_rpi_pisp.so`, and `python3-picamera2` isn't in the noble
archive (the only workaround is building Raspberry Pi's libcamera fork from
source). Ubuntu 25.04+ fixed this properly: PiSP in libcamera plus packaged
`rpicam-apps`/`python3-picamera2`. Retargeted to **Ubuntu 26.04 (Resolute) +
ROS2 Lyrical Luth** — both LTS (Lyrical supported to May 2031). Lyrical is two
months old, so spike step 8 must confirm the `ros-lyrical-*` packages we need
actually exist before committing.

### Two refinements made while writing this plan (flagging for owner sign-off)

1. **"Hub becomes a bridge" → "nodes dual-publish".** The agreed intent was
   "dashboard untouched". The cheapest correct way to get that is *not* to teach
   the hub to subscribe to DDS and rebuild snapshots — it is to keep the existing
   `telemetry.socket_client.publish_message(...)` calls in the ported services.
   Each migrated node publishes ROS messages **and** keeps its current hub
   publish. The hub, both dashboards, and all snapshot arbitration
   (`_prefer_motion`, stale handling, motor-battery cache) are literally
   unchanged — zero new code, zero new failure modes. The dual publish is the
   temporary scaffolding, and it dies whenever the dashboard is eventually
   rebuilt on Foxglove/rosbridge. **But note the hub outlives the dashboard:**
   after this plan the hub still feeds the motion safety gate (`/range` data),
   the turn intents (IMU yaw), and the LLM's `check_health` /
   `check_surroundings`. The hub can only be retired after those inputs move
   to ROS topics / a ROS-backed source — which this plan deliberately does
   not do, and which nothing currently forces. The end state here is a stable
   hybrid, not a fully ROS robot.
2. **No colcon workspace / ament package yet.** Everything we need runs without
   one: nodes are plain Python files started by systemd (`ros2 launch` accepts a
   bare file path for dev use; `robot_state_publisher`, `foxglove_bridge`, and
   later `slam_toolbox` all come from apt). A package buys us `ros2 run`
   registration, package-relative resources, and custom messages — none of which
   we use. **Trigger to add one** (mechanical, ~30 lines of boilerplate): custom
   message/service types, or a third-party stack that demands package-relative
   installs. Until then: no `package.xml`, no `setup.py`, no build step in
   deploy — `web-deploy` stays rsync + restart.

## Target topology

```
                          (plain services, unchanged)
  robot-voice ── motion-intent.sock ──┐        robot-camera / robot-vision
  robot-telemetry hub ◄── dual-publish┼── robot-web-dashboard / robot-dashboard
                                      │
  ┌──────────────── ROS2 (localhost-only DDS) ────────────────┐
  │ gamepad_teleop_node ──► /cmd_vel (geometry_msgs/Twist)    │
  │                              │ 0.5 s deadman              │
  │                              ▼                            │
  │ motion_node ──► /odom (nav_msgs/Odometry) + TF odom→base_link
  │             ──► /battery/motor (sensor_msgs/BatteryState) │
  │             ◄── /range/*, /imu (safety gate inputs)       │
  │ sensors_node ──► /range/<name> (sensor_msgs/Range) ×5     │
  │              ──► /imu (sensor_msgs/Imu)                   │
  │ pi_battery_node ──► /battery/pi (BatteryState)            │
  │ robot_state_publisher ──► TF base_link→sensor frames (URDF)│
  │ foxglove_bridge ──► ws://robot:8765 (Mac Foxglove app)    │
  └──────────────────────────────────────────────────────────┘
```

Conventions: REP-103 throughout (already true of `robot_model`, odometry, and
kinematics). Frame names: `odom` → `base_link` → sensor frames named exactly as
`robot_model.SENSOR_MOUNTS` / `sensors.json` (`forward_left`, `cliff_right`, …).
DDS stays off the Wi-Fi: `ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST` and a fixed
`ROS_DOMAIN_ID`; the Mac talks to the robot through Foxglove's websocket, not DDS.

## Safety invariant (applies to Phases 4–5, verify at each)

Today `robot-motion` stops when the drive command goes stale (0.5 s) or when
`controller_reader_alive` arrives false. After migration the equivalent must
hold: **if the teleop node dies, hangs, or the controller reader stops, the
wheels stop within 0.5 s.** The mechanism is a `/cmd_vel` deadman in the motion
node (same 0.5 s constant) plus teleop publishing an explicit zero twist on its
way down. The safety gate (cliff/forward sensor blocking) and the slew shaper
stay inside the motion node, in-process, exactly as they are.

## Baseline

Keep the full suite green on the Mac after every phase:

```bash
python3 -m unittest discover tests
```

---

## Phase 0 — Image the Trixie card, flash Ubuntu, hardware spike (go/no-go)

### Why

Everything downstream assumes Ubuntu 26.04 works on this robot's hardware. The
spike proves camera, audio, UART, I2C, and gamepad before we commit. The current
SD card is imaged first so failure costs nothing but time.

### 0a. Back up the current card (on the Mac)

Pull the card, put it in the Mac's reader, then:

```bash
# 1. Find the card's disk number — look for the card by size; it will show
#    partitions like "bootfs"/"rootfs". Assume /dev/disk4 below; SUBSTITUTE YOURS.
diskutil list

# 2. Unmount (not eject) all its volumes
diskutil unmountDisk /dev/disk4

# 3. Image the whole card, compressed. rdisk (raw) is much faster than disk.
#    Ctrl+T prints progress if the status line is quiet. Expect ~10-30 min.
sudo dd if=/dev/rdisk4 bs=4m status=progress | gzip > ~/robopet-trixie-$(date +%Y%m%d).img.gz

# 4. Sanity-check the image is non-trivial in size, then eject
ls -lh ~/robopet-trixie-*.img.gz
diskutil eject /dev/disk4
```

**Restore procedure** (the rollback, if the spike fails — writes the image back
over the card; verify the disk number again first, dd to the wrong disk is
unrecoverable):

```bash
diskutil list
diskutil unmountDisk /dev/disk4
gunzip -c ~/robopet-trixie-YYYYMMDD.img.gz | sudo dd of=/dev/rdisk4 bs=4m status=progress
diskutil eject /dev/disk4
```

Ideally use a **second SD card** for Ubuntu and never write over the Trixie
card at all — then rollback is just swapping cards and the image is belt-and-
suspenders.

### 0b. Flash Ubuntu

Raspberry Pi Imager (already in `artifacts/`): choose **Ubuntu Server 26.04 LTS
(64-bit)** under Other general-purpose OS. In the imager's customization set
hostname, user `pi`, Wi-Fi, and enable SSH — that replicates what
`initialize-pi.sh` expects.

### 0c. Spike checklist (on the Pi, by hand — nothing scripted yet)

Work through these in order; each is a go/no-go item. Take notes — Phase 1
turns the notes into `setup.sh`.

1. **Boot config parity.** Ubuntu uses the same `/boot/firmware/config.txt`.
   Add `enable_uart=1`, `dtoverlay=disable-bt`, `dtparam=i2c_arm=on`.
   **Ubuntu-specific trap:** Ubuntu puts a serial console on the UART. Remove
   `console=serial0,115200` (or `console=ttyAMA0,...`) from
   `/boot/firmware/cmdline.txt` and
   `sudo systemctl disable --now serial-getty@ttyAMA0.service`, or the RoboClaw
   port is owned by a login shell. Reboot.
2. **RoboClaw UART.** Groups (`dialout`), then from a venv with `basicmicro`:
   read firmware version / battery voltage over `/dev/serial0`, and run
   `scripts/test-motor.py` with wheels off the ground.
3. **I2C.** `sudo apt install i2c-tools`, add user to `i2c` group (create the
   group + a udev rule if Ubuntu's image lacks it), `i2cdetect -y 1` shows the
   TCA9548A at 0x70. Then a venv with the adafruit packages reads one VL53 and
   the IMU.
4. **Camera.** `sudo apt install python3-picamera2` — in the Ubuntu archive
   since 25.04 (PiSP landed in Ubuntu's libcamera; this was the blocker that
   killed the 24.04 target). `rpicam-hello` first to prove the sensor, then
   import `Picamera2` and grab a frame from the `--system-site-packages` venv.
5. **ReSpeaker audio.** `sudo apt install alsa-utils`, install the udev rule
   from `setup.sh`, `arecord -l` / `aplay -l` show the device; record and play a
   clip; set PCM volume via `amixer`.
6. **Gamepad.** `input` group, evdev sees the Xbox controller.
6b. **GPIO (motor-rail MOSFET + status LEDs).** `robot_battery.py` and
   `status_leds.py` use `gpiozero`, which on Raspberry Pi OS rides the
   preinstalled `lgpio` backend — pip gpiozero on Ubuntu has no backend and
   the `RPi.GPIO` fallback does not support the Pi 5. Confirm a venv with
   `gpiozero` + `lgpio` (pip, or apt `python3-lgpio`) can toggle a safe spare
   pin. The low-LiPo cutoff depends on this; it blocks.
6c. **Venv + wheels for the new Python.** 26.04's Python is newer than 3.12;
   the wake-word path needs `onnxruntime`, which lags new Pythons on aarch64.
   Build the real venv: `pip install -e .` plus the openwakeword line from
   `setup.sh`, then `python -m unittest discover tests` on the Pi. Fails fast
   here instead of mid-Phase-1.
7. **Housekeeping equivalents.** `vcgencmd` is Pi-OS; confirm the telemetry
   fallback path `/sys/class/thermal/thermal_zone0/temp` exists (it does — the
   hub already handles this). Check whether the `rpi-eeprom` package is
   available for `POWER_OFF_ON_HALT`; if not, note it and move on (nice-to-have).
8. **ROS2 Lyrical Luth.** Install per current docs (verify the method — this changed in
   2025 to a `ros2-apt-source` deb):

   ```bash
   sudo apt install -y software-properties-common curl
   sudo add-apt-repository universe
   ROS_APT_SOURCE_VERSION=$(curl -s https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest | grep -F "tag_name" | awk -F\" '{print $4}')
   curl -L -o /tmp/ros2-apt-source.deb "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${ROS_APT_SOURCE_VERSION}/ros2-apt-source_${ROS_APT_SOURCE_VERSION}.$(. /etc/os-release && echo $VERSION_CODENAME)_all.deb"
   sudo dpkg -i /tmp/ros2-apt-source.deb
   sudo apt update
   sudo apt install -y ros-lyrical-ros-base ros-lyrical-demo-nodes-py ros-lyrical-foxglove-bridge
   ```

   Then: `source /opt/ros/lyrical/setup.bash`, run `ros2 run demo_nodes_py talker`
   and `listener` in two shells. Also confirm the venv interop: a
   `--system-site-packages` venv whose shell sourced the ROS setup can
   `import rclpy` **and** `import numpy` from the venv.
9. **Foxglove smoke test.** `ros2 run foxglove_bridge foxglove_bridge`, connect
   the Mac Foxglove app to `ws://<pi>:8765`, see the talker topic.

### Acceptance / go-no-go

All nine items pass → go. Camera unsolvable → stop, restore/swap the Trixie
card, revisit the Docker option. Anything else failing is negotiable — decide
per item whether it blocks (RoboClaw/I2C do; EEPROM doesn't).

---

## Phase 1 — Port setup.sh and provisioning to Ubuntu (pre-ROS parity)

### Why

Before any node exists, the robot should run **exactly today's eleven services
on Ubuntu**. That isolates "OS port" bugs from "ROS port" bugs.

### Work

1. Rewrite `setup.sh` from the spike notes: apt list changes (camera packages
   per the spike outcome; drop/keep `opencv-data` as Ubuntu provides), the
   serial-console removal from step 0c-1 (idempotent edit of `cmdline.txt` +
   getty mask), i2c group/udev if needed, EEPROM step guarded by
   `command -v` as it already is.
2. Add the ROS2 install to `setup.sh` as a new step: the apt-source deb,
   `ros-lyrical-ros-base`, `ros-lyrical-foxglove-bridge`,
   `ros-lyrical-robot-state-publisher`, `ros-lyrical-demo-nodes-py` (kept for
   smoke-testing). Idempotent like every other step.
3. Add `/home/pi/.config/robot-pet/ros.env` written by setup:

   ```
   ROS_DOMAIN_ID=17
   ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
   ```

4. Add `scripts/ros-node.sh` — the single wrapper every ROS unit will use:

   ```bash
   #!/bin/bash
   # Run a robot-pet ROS node: ROS env + repo venv python.
   set -euo pipefail
   source /opt/ros/lyrical/setup.bash
   set -a; source /home/pi/.config/robot-pet/ros.env; set +a
   exec /home/pi/robot-pet/.venv/bin/python "$@"
   ```

5. `initialize-pi.sh`: update for the Ubuntu image's first-boot differences
   found in the spike (imager customization covers most of it). Ubuntu-server
   specifics: run `cloud-init status --wait` before the apt step (cloud-init
   and unattended-upgrades hold the apt lock on first boot), and export
   `DEBIAN_FRONTEND=noninteractive` / `NEEDRESTART_MODE=a` so `needrestart`
   doesn't block the scripted upgrade with an interactive prompt.
6. Run full `setup.sh` on the Ubuntu card. All existing services come up;
   dashboard at `:8080` works; drive the robot with the gamepad; voice
   round-trips.

### Tests / acceptance

`python3 -m unittest discover tests` passes **on the Pi** (the suite runs there
during setup already). Dashboard shows live wheels/sensors/camera/voice.
Gamepad drives. This is the parity milestone — tag it in git
(`pre-ros2-ubuntu-parity`) so every later phase has a known-good fallback.

---

## Phase 2 — ROS plumbing skeleton: env, units pattern, Foxglove

### Why

Get the boring integration risk (systemd + venv + ROS env + discovery config)
out of the way with a node that does nothing important, and get Foxglove up so
every subsequent phase is observable from the Mac.

### Work

1. Create `src/ros_nodes/` with one trivial node, `heartbeat_node.py` (~25
   lines): publishes `std_msgs/String` on `/robot/heartbeat` at 1 Hz. This is
   scaffolding to prove the unit pattern; it is deleted in Phase 7.
2. Add `systemd/robot-foxglove.service`:
   `ExecStart=/bin/bash -c 'source /opt/ros/lyrical/setup.bash && … foxglove_bridge'`
   (or via `ros-node.sh`'s pattern), `Restart=always`, same journald setup as
   the rest.
3. Add `systemd/robot-heartbeat.service` using `scripts/ros-node.sh`.
4. Wire both into `setup.sh` (install/enable), `restart.sh` (names + order:
   foxglove anywhere late; heartbeat anywhere), `redeploy-robot.sh`
   (`src/ros_nodes/*` → restart the ROS units), and the sudoers allowlist.
5. Update the sudoers generation in `setup.sh` to a loop over a service list —
   the current copy-paste block triples with new units (this is the third use;
   the abstraction is now allowed).

### Tests / acceptance

- From the Mac: Foxglove connects, `/robot/heartbeat` ticks.
- `sudo systemctl kill robot-heartbeat` → systemd restarts it → heartbeat
  resumes (proves Restart=always through the wrapper).
- `./restart.sh heartbeat foxglove` works. Mac test suite untouched and green
  (nothing imports `rclpy` off-robot).

---

## Phase 3 — Sensors node: `/range/*` and `/imu`

### Why

Lowest-risk real port (no actuators), and it produces the topics the motion
node's safety gate will eventually consume. `SensorsService` already takes an
injected `publish` callable, so the service logic ports without modification.

### Work

1. `src/ros_nodes/sensors_node.py`: an rclpy node that owns a `SensorsService`
   exactly as `robot_sensors.main()` does, but its `publish` callback **both**
   sends the existing hub message (dual-publish) **and** publishes:
   - one `sensor_msgs/Range` per reading on `/range/<name>`, `frame_id=<name>`,
     `radiation_type=INFRARED`, min/max range and FOV per sensor kind (VL53L0X
     cliff vs VL53L1X forward — constants in the node file), `range` in meters
     (readings are mm today), NaN/`+inf` handling for failed reads per REP-117.
   - `sensor_msgs/Imu` on `/imu` (orientation-only quaternion from the
     yaw/pitch/roll degrees; leave angular velocity/acceleration covariance
     marked unknown = -1).
2. Keep the message-building logic (reading dict → Range fields, ypr →
   quaternion) in a plain module `src/ros_nodes/ros_messages.py` **that imports
   only `std/sensor/geometry/nav` msg classes lazily or not at all** — build
   plain dict/tuple intermediates so the conversion math is unit-testable on the
   Mac without rclpy. (Message classes without rclpy are importable only on the
   Pi; keep Mac tests to the pure math: mm→m, quaternion, covariance layout.)
3. New unit `systemd/robot-sensors-ros.service`? **No** — replace the ExecStart
   of the existing `robot-sensors.service` to run `sensors_node.py` via
   `ros-node.sh`. Same name, same journal, same restart semantics. `robot_sensors.py`
   stays for one phase as fallback, then dies in Phase 7.
4. Update `redeploy-robot.sh` mappings (`src/ros_nodes/sensors_node.py`,
   `ros_messages.py` → robot-sensors.service).

### Tests / acceptance

- Mac: new `tests/test_ros_messages.py` for the pure conversions (quaternion
  from ypr, Range field mapping, REP-117 out-of-range encoding). Suite green.
- Pi: `ros2 topic hz /range/cliff_left` ≈ configured poll rate; Foxglove shows
  all five ranges + IMU; dashboard sensors panel unchanged (dual-publish);
  motion's safety gate still trips (it still reads the hub — unchanged).

---

## Phase 4 — Motion node: `/cmd_vel` in, `/odom` + TF out

### Why

The heart of the migration. `MotionRunner` already isolates every hard problem
(slew, safety, intents, encoder moves, reconnect); the node is a shell around it.

### Work

1. `src/ros_nodes/motion_node.py` wraps `MotionRunner`. `rclpy.spin` runs in a
   thread (like the telemetry subscribe loop today); the motor loop stays the
   owner of timing. Changes inside `robot_motion.py` are surgical:
   - **Input seam:** add a twist path beside the drive-socket path. A
     `/cmd_vel` subscription stores `(MotionCommand, stamp)`;
     `_get_drive_command()` grows a sibling `_get_twist_command()` with the
     same `DRIVE_COMMAND_STALE_SECONDS` deadman. Conversion is one call to
     `body_twist_to_wheel_qpps` (prep Phase 4). While both paths exist, the
     socket wins if present (lets Phase 4 land before Phase 5 flips teleop).
   - **Wheels telemetry:** when driven by twist there is no `drive.wheels`
     passenger; compute `left_command`/`right_command` as `qpps / config.qpps`.
   - **`controller_reader_alive`:** the twist path has no such field; the
     deadman covers it (see safety invariant). The socket-path check stays
     until Phase 5 removes the socket.
   - **Gamepad-active arbitration for intents:** twist ≠ zero within the
     deadman window ⇒ gamepad active (same semantics as nonzero wheels today).
   - **Outputs:** publish `nav_msgs/Odometry` on `/odom` (pose from
     `self._odometry.pose`, twist from `wheel_qpps_to_body_twist(actual qpps)`,
     fixed diagonal covariance constants) and broadcast TF `odom → base_link`
     at their **own named rate constant** (`ODOM_PUBLISH_HZ = 5` for now — do
     NOT piggyback on the hub telemetry cadence; slam_toolbox interpolates
     robot pose from this TF for every lidar scan, and the RPLidar spins at
     ~10 Hz, so this constant goes to 20–30 Hz when the lidar lands — smearing
     scans during in-place turns otherwise). `sensor_msgs/BatteryState` on
     `/battery/motor` from the pack voltage stays at the existing
     `_publish_telemetry` cadence. Message assembly lives in
     `ros_messages.py` (pure, Mac-tested).
   - Keep: hub publishes (dual-publish), motion-intent socket (voice is out of
     scope and keeps working), sensors safety via the hub subscription
     (switching safety input to `/range/*` topics is deliberately deferred —
     smaller change, and the hub feed is unchanged and live).
2. `robot-motion.service` ExecStart → `ros-node.sh …/motion_node.py`.
3. `redeploy-robot.sh` mappings.

### Tests / acceptance

- Mac: extend `tests/test_robot_motion.py` — twist-path deadman (fresh twist
  drives, stale twist stops), twist→qpps sign convention (positive angular_z ⇒
  right wheel faster), wheels payload from qpps, odometry payload unchanged.
  All existing tests untouched and green.
- Pi (wheels up): `ros2 topic pub -r 20 /cmd_vel geometry_msgs/msg/Twist
  '{linear: {x: 0.2}}'` spins the wheels; killing the pub stops them ≤ 0.5 s
  (watch `/odom` twist). Voice "move forward" intent still works. Foxglove
  shows the robot's pose track under teleop. Dashboard unchanged.

---

## Phase 5 — Teleop node: gamepad → `/cmd_vel`, drive socket retired

### Why

Completes the command path. After this, `/cmd_vel` is the only way wheels move
(intents excepted), which is exactly the seam slam/nav2 will use.

### Work

1. `src/ros_nodes/gamepad_teleop_node.py` wraps `GamepadTeleopRunner` with one
   behavioral change: instead of `motion.send(DriveCommand(...))`, compute
   `to_wheel_speeds` exactly as today (preserving deadzone/turn/turbo feel and
   tuning config), convert with `wheel_qpps_to_body_twist`, publish `Twist` on
   `/cmd_vel` at the same 20 Hz. Keep all hub telemetry publishes
   (`gamepad_update`, `gamepad_teleop_update`) as they are.
2. Shutdown/reader-death behavior: on `reader_alive() == False`, controller
   disconnect, or SIGTERM, publish one zero twist, then stop publishing (the
   motion deadman is the backstop). This preserves today's stop guarantees —
   re-verify the safety invariant explicitly.
3. Remove the drive-socket path: `MotionDrivePublisher` use in teleop,
   `DriveCommandListener` + socket-path branches in `robot_motion.py`, and the
   now-dead `DriveCommand` passengers. `control/motion_drive.py` shrinks or
   dies (`motion_intent.py` is unaffected). This finishes what prep Phase 5
   deliberately deferred — the socket scaffolding is now actually replaced.
4. `gamepad-teleop.service` ExecStart → `ros-node.sh`; redeploy mappings; drive
   tuning config keeps its current file + restart flow (dashboard tuning panel
   unchanged; ROS parameters are a later nicety, not this plan).

### Tests / acceptance

- Mac: `tests/test_gamepad_teleop.py` — runner produces the expected twist
  sequence for scripted stick states (inject a fake publisher); zero-twist on
  reader death. `tests/test_robot_motion.py` — socket path gone, twist path
  covers stop/deadman/safety. Round-trip: stick → qpps → twist → qpps identity
  within rounding (prep tests already cover the kinematics core).
- Pi: drive the robot end-to-end by gamepad through DDS. Kill
  `gamepad-teleop.service` mid-drive → wheels stop ≤ 0.5 s. Yank the controller
  USB dongle mid-drive → same. Dashboard wheel/controller panels live as before.

---

## Phase 6 — URDF, robot_state_publisher, battery states

### Why

The TF tree is what the lidar/SLAM actually consumes: `odom → base_link`
(Phase 4) plus static `base_link → <sensor>` frames. `robot_model.py` was built
to be this file's single source — so generate, don't hand-write.

### Work

1. `scripts/generate-urdf.py`: a small script that imports `robot_model` and
   emits `urdf/robot.urdf` — `base_link` (box from the footprint constants),
   fixed joints to each `SENSOR_MOUNTS` entry (xyz + rpy from pitch/yaw), wheel
   links at `±TRACK_WIDTH_METERS/2` with `WHEEL_RADIUS_METERS`, and a
   `laser` frame placeholder **left commented** until the lidar mount is
   measured. Check the generated file in; regenerating is a script run, and the
   test below keeps it honest.
2. `systemd/robot-state-publisher.service`: apt `robot_state_publisher` with
   `robot_description` from the generated file (same bash-source ExecStart
   pattern as foxglove). setup/restart/redeploy/sudoers wiring.
3. `src/ros_nodes/pi_battery_node.py`: wrap the existing pi-battery service
   loop, dual-publish + `sensor_msgs/BatteryState` on `/battery/pi` (charge,
   voltage, percentage, `power_supply_status` from the charging flags).
   `robot-pi-battery.service` → `ros-node.sh`. (`/battery/motor` already landed
   with Phase 4; `robot_battery.py` motor-rail service is GPIO policy, stays
   plain.)

### Tests / acceptance

- Mac: `tests/test_generate_urdf.py` — generated URDF parses (xml), one frame
  per sensor mount with matching xyz/rpy, wheels at ±track/2; regeneration is
  idempotent (`git diff --exit-code urdf/` after running the script).
- Pi: `ros2 run tf2_tools view_frames` (or Foxglove's TF panel) shows
  `odom → base_link → {forward_*, cliff_*, wheels}`; ranges render at their
  mounts in Foxglove's 3D panel; `/battery/*` topics live; dashboard battery
  panels unchanged.

---

## Phase 7 — Retire scaffolding, docs, lidar-readiness

### Work

1. Delete: `heartbeat_node.py` + its unit, `robot_sensors.py` /
   `gamepad_teleop.py` / `robot_pi_battery.py` `main()` entrypoints that the
   nodes superseded (keep the classes the nodes import — only the dead
   `if __name__` service shells go), `robot_motion.py`'s removed socket branches
   if any stubs remain, and prep-plan comments that said "migration-time work".
2. `docs/ARCHITECTURE.md`: rewrite the service table and migration section to
   describe reality (which services are ROS nodes, the dual-publish contract,
   the env/wrapper pattern, Foxglove). Update the `telemetry/messages.py`
   migration-map comment: mark the ported messages as ported.
3. `README.md` / `docs/gamepad-teleop.md` touch-ups where they reference the
   drive socket.
4. Write the **lidar-readiness checklist** at the bottom of this plan's doc or
   ARCHITECTURE.md: apt `ros-lyrical-slam-toolbox`; a lidar driver node
   publishing `/scan` with `frame_id=laser`; uncomment + measure the `laser`
   mount in `robot_model`/URDF; slam_toolbox launch under one new systemd unit;
   Foxglove map panel; raise `ODOM_PUBLISH_HZ` to 20–30 (see Phase 4). Nothing
   else should need to move — that claim is the migration's exit criterion.
   The checklist must also carry these three known-future items so the exit
   criterion stays honest:
   - **`/cmd_vel` arbitration.** Phase 4's "twist ≠ zero ⇒ gamepad active"
     rule is correct only while teleop is the sole `/cmd_vel` publisher. The
     moment nav2 (or any autonomous commander) publishes too, a nav goal
     preempts every voice intent and an intent fights nav tick-by-tick. Fix at
     that point: `twist_mux` (apt) with prioritized inputs
     (`/cmd_vel_teleop` > `/cmd_vel_nav`, …) feeding one `/cmd_vel`, plus a
     decision about where motion intents rank.
   - **Map persistence.** slam_toolbox's serialized map + localization mode
     (or map_saver). Without it the `map` frame is rebuilt from scratch each
     boot and any saved landmark/place poses ("kitchen") are garbage across
     reboots.
   - **Async nav goals for the LLM.** See
     [2026-07-13 async-nav-goals plan](2026-07-13%20-%20async-nav-goals.md) —
     `navigateTo(place)` cannot ride the blocking 35 s motion-intent
     request/reply.

### Acceptance

Full Mac suite green. Full Pi suite green. `setup.sh` from a fresh Ubuntu flash
brings up the whole robot including ROS units (this re-run is the real test of
idempotency). Gamepad drive, voice intents, dashboard, Foxglove all live
simultaneously for a 30-minute soak with no unit restarts in `journalctl`.

---

## Out of scope

- The lidar driver, slam_toolbox, nav2, and any autonomy (next plan).
- Porting voice, camera, vision, brain, telemetry hub, or either dashboard.
- IMU fusion (`robot_localization`) — wheel odom only, as prepped.
- ROS parameters for drive tuning; custom ROS message types; colcon/ament
  packaging (each has an explicit trigger noted above).
- Multi-machine DDS (Mac joins via Foxglove websocket only).

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Camera regression on Ubuntu (was the 24.04 killer; 26.04 packages picamera2) | Phase 0 step 4 go/no-go before anything is committed; Trixie image = free rollback; fallback is revisiting Docker on Raspberry Pi OS. |
| gpiozero has no working backend on Ubuntu/Pi 5 (motor-rail cutoff depends on it) | Phase 0 step 6b go/no-go; fix is `lgpio` in the venv. |
| pip wheels missing for 26.04's Python (esp. onnxruntime for wake word) | Phase 0 step 6c builds the real venv and runs the suite on the Pi. |
| Lyrical is two months old — needed `ros-lyrical-*` packages not yet released | Phase 0 step 8 verifies ros-base/foxglove_bridge (and slam-toolbox for later) resolve before committing. |
| ROS install method drifted since planning | Phase 0 step 8 says verify against current docs before running. |
| Wheels-keep-spinning regression | Safety invariant section; explicit kill-the-publisher tests in Phases 4 and 5 acceptance. |
| Dashboard silently degrades | Dual-publish means dashboard code paths are untouched; each phase's acceptance includes a dashboard check. |
| rclpy leaks into Mac tests | Node files are the only rclpy importers; pure conversion math lives in `ros_messages.py`/existing layers; suite runs on the Mac every phase. |
| Pi CPU/RAM pressure from DDS + existing load | LOCALHOST-only discovery, odom/TF at `ODOM_PUBLISH_HZ` (5 for now), no image topics. Watch the dashboard's pi panel during the Phase 7 soak. |

## Done criteria

- The robot drives by gamepad and voice with `/cmd_vel` as the only wheel-speed
  path, and stops within 0.5 s of losing its commander.
- `/odom`, TF (`odom→base_link→sensors`), `/range/*`, `/imu`, `/battery/*` are
  live and visible in Foxglove from the Mac.
- Web dashboard, SSH TUI, voice, camera, and vision behave exactly as before.
- `setup.sh` provisions a fresh Ubuntu card end-to-end; `restart.sh` and
  web-deploy handle every unit including the ROS ones.
- `python3 -m unittest discover tests` is green on the Mac with no ROS installed.
- Adding the lidar requires only: driver node + `/scan`, one URDF line, one
  slam_toolbox unit.
