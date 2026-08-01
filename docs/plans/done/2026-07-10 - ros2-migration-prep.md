# ROS2 Migration Prep Plan

Goal: do the framework-agnostic groundwork that makes the eventual ROS2 + lidar +
SLAM migration a transcription job instead of an archaeology job. **We are not
adding ROS2 in this plan.** The code phases land as plain Python in `src/drivers/`
and `src/control/` — the layers `docs/ARCHITECTURE.md` promises will survive the
migration unchanged — plus small, backward-compatible additions to the telemetry
payloads. The bookend phases (0 and 6) are docs-only.

Why now: the driver and control layers already held up well (pure Python, factory
injection, good tests, `MotionCommand` is already Twist-shaped). The gaps are the
things SLAM specifically needs and that nothing has forced us to build yet:

1. There is no single source of truth for the robot's physical geometry. Wheel
   diameter lives in `robot_motion.py`, track width is defined *nowhere*, and sensor
   mount poses live in prose (`docs/robot dimensions.md`,
   `docs/forward tof sensor geometry.md`). A URDF needs all of it in one place.
2. Odometry is wheel-travel only. `robot-motion` publishes cumulative left/right
   distance but never integrates a pose `(x, y, theta)`. SLAM and nav both want a
   real odom source.
3. The drive command speaks encoder counts (`qpps`), not velocity. ROS2 wants
   `/cmd_vel` as a `Twist` in m/s and rad/s.

Related docs: [ARCHITECTURE.md](../ARCHITECTURE.md),
[robot dimensions.md](../robot%20dimensions.md),
[forward tof sensor geometry.md](../forward%20tof%20sensor%20geometry.md),
[roboclaw-encoder-distance.md](2026-06-20%20-%20roboclaw-encoder-distance.md)
(where the encoder constants came from).

Follow `CLAUDE.md`: minimal, flat, no new abstractions or manager classes. Every
new piece here is pure functions plus a couple of frozen dataclasses. If a change
seems to need a service layer or a factory, stop and reconsider.

## Status (2026-07-10)

- **Phase 0 — done.** Both geometry docs rewritten to REP-103.
- **Phase 1 — done.** `src/robot_model.py` created with measured values, and
  `robot_motion.py` imports `ENCODER_COUNTS_PER_METER` from it. Resolved facts for
  the remaining phases: `base_link` = drive-axle midpoint at ground level, axle
  55 mm behind the bumper; `TRACK_WIDTH_METERS = 0.306`; two cliff sensors (no
  center); cliff pitch is **positive** 0.611 rad (nose-down in a z-up frame).
- **Phase 2 — done.** `src/control/odometry.py` created — pure `DiffDriveOdometry`
  (exact-form midpoint dead reckoning) plus `Pose`, with `tests/test_odometry.py`.
  No hardware wiring yet; track width is a constructor argument.
- **Phase 3 — done.** `robot_motion.py` feeds the per-tick wheel deltas into a
  `DiffDriveOdometry` and publishes `x`/`y`/`theta` as additive keys on
  `odometry_message` (legacy distance keys unchanged; dashboard/`robot_telemetry`
  ignore the new keys). **Deviation from step 3 below:** `reset()` is intentionally
  *not* wired into `_invalidate_odometry_baseline` — that fires on transient read
  failures and keeps the cumulative distance, so resetting the pose would violate the
  "pose must not jump" criterion. Pose stays continuous across re-baselines exactly
  like the distance counters; a true zero only happens on process restart (a fresh
  `DiffDriveOdometry` at the origin).
- **Phase 4 — done.** `differential_drive.py` gained `body_twist_to_wheel_qpps` and
  `wheel_qpps_to_body_twist` (free functions, REP-103 signs matching
  `DiffDriveOdometry`) with round-trip / pure-rotation / pure-translation tests. The
  legacy normalized `mix` / `to_wheel_speeds` (opposite gamepad sign) are untouched;
  nothing is rewired to `/cmd_vel` yet — that is migration-time work.
- **Phase 5 — done (scoped down, owner-reviewed).** Only the provably-dead
  passengers were removed from `DriveCommand`: `controller` and `drive_tuning`.
  Audit showed `robot-motion` never reads or republishes either — the dashboard gets
  both straight from gamepad-teleop's own `gamepad_teleop_update` (`robot_telemetry.py`
  lines 256/261), so dropping them is behavior-neutral. **Deliberately deferred:**
  moving `wheels` / `drive_status` / `link_loop` off the command. `wheels` (gamepad-
  active arbitration) and `drive_status.controller_reader_alive` (the safety stop) are
  command-critical and must stay; `link_loop` + the rest of `drive_status` are
  republished-through-motion and consumed live by the dashboard via `_prefer_motion`,
  so rerouting them is high-churn work on socket scaffolding the migration replaces
  wholesale. Not worth the risk now (the plan itself sanctions deferring when in doubt).
- **Phase 6 — done.** Added a central "ROS2 migration map" comment block to
  `telemetry/messages.py` naming the target ROS2 message for each real payload and
  flagging the rest as scaffolding. No behavior change.

## Coordinate conventions (READ FIRST — every phase depends on this)

We adopt **ROS REP-103** now so the migration is free. All new code uses these:

- Right-handed frame fixed to the robot body (`base_link`).
- **+x is forward, +y is left, +z is up.**
- **Positive yaw (theta) is counterclockwise** — i.e. a left turn increases theta.
- Angles in **radians**, distances in **meters**, speeds in **m/s** and **rad/s**.

Today `docs/forward tof sensor geometry.md` uses an ad-hoc convention where
"positive x is right" — the **opposite** of REP-103's +y-left. That convention lives
*only* in the markdown (no code or config encodes any sensor position), so **Phase 0
rewrites the docs into REP-103 first.** After Phase 0 there is one common base and the
Phase 1 transcription is a straight copy with no sign flips to remember.

## Baseline

Run the focused suite before touching anything, and keep it green after every phase:

```bash
python3 -m unittest tests.test_motor tests.test_range tests.test_motion_intent tests.test_robot_motion tests.test_robot_sensors tests.test_control
```

New test modules introduced by this plan (`tests.test_robot_model`,
`tests.test_odometry`) get added to that command as they land.

---

## Phase 0 — Normalize the geometry docs to REP-103

### Why

The physical geometry the URDF needs currently lives in prose, and one doc uses a
handedness (+x = right) opposite to the ROS standard we are adopting. This convention
exists *only* in markdown — confirmed: no code or config encodes any sensor x/y
position — so we can fix it at the source cheaply, before anything transcribes it.
Starting every later phase from one common, understandable base beats carrying a
"remember to flip the sign" caveat.

### Work

Edit the geometry docs so all positions are stated in REP-103 (**+x forward, +y left,
+z up, meters, radians, CCW-positive yaw**). This is documentation only — no code
changes.

1. `docs/forward tof sensor geometry.md`: replace the "Coordinate system" section's
   "negative x is left / positive x is right" with the REP-103 frame, and restate the
   three mounting positions in `base_link`:
   - left sensor: the doc's "x = -110 mm" (left side) → **y = +0.110 m**
   - right sensor: the doc's "x = +110 mm" → **y = -0.110 m**
   - center: **y = 0**
   Keep the coverage/FOV math intact (it is about cone width, not handedness), but if
   any formula reads a signed x, make sure it still reads correctly after the rename —
   simplest is to keep the coverage math in terms of magnitude and lateral offset and
   just fix the frame description. The `base_link` x-origin is the drive axle (55 mm
   behind the bumper front face); express the bumper inset `S` as `x = 0.055 - S`.
2. `docs/robot dimensions.md`: add explicit REP-103 coordinates for the downward
   cliff ToFs alongside the existing prose (128 mm off center → **y = ±0.128 m**,
   67 mm off ground → **z = 0.067 m**, angled 35° down → **pitch = +0.611 rad**
   (positive pitch is nose-down in a z-up frame: a positive rotation about +y tilts
   the forward axis toward -z). Leave the raw measurements in;
   just add the frame-referenced values so Phase 1 is a copy.
3. Add a one-line note at the top of each doc: "Positions are in `base_link`
   (REP-103: +x forward, +y left, +z up)."

### Tests

None (docs only). Sanity check: a reader unfamiliar with the robot can tell left from
right from the coordinates alone, without knowing the old convention.

### Acceptance

Both docs state every sensor position in REP-103 with no remaining "+x = right"
language. Phase 1 can transcribe numbers directly with no sign conversion.

---

## Phase 1 — Single source of truth for the physical robot model

### Why

The URDF, the odometry math (Phase 2), and the velocity kinematics (Phase 4) all
need the same physical constants. Today they are split between an inline constant in
`robot_motion.py` and two markdown docs. Consolidate them into one pure module so
later work — and the URDF — reads from one place.

### Work

Create `src/robot_model.py` (a new top-level, framework-agnostic module — importable
as `robot_model`, same as `control` and `drivers`). No hardware imports, no framework
imports. It holds only constants and small frozen dataclasses.

1. Move the drive-train constants out of `src/robot_motion.py:63-65` into this module:
   - `WHEEL_DIAMETER_METERS = 0.096`
   - `WHEEL_RADIUS_METERS = WHEEL_DIAMETER_METERS / 2`
   - `ENCODER_COUNTS_PER_WHEEL_REVOLUTION = 537.7`
   - `ENCODER_COUNTS_PER_METER = ENCODER_COUNTS_PER_WHEEL_REVOLUTION / (math.pi * WHEEL_DIAMETER_METERS)`
2. Add `TRACK_WIDTH_METERS`. This is simply not recorded yet — **measure it on the
   robot** (caliper/tape across the two wheels, contact-patch center to contact-patch
   center) and put the real number in. Do not invent a placeholder; the value is a
   direct measurement.
   ```python
   # Measured wheel-contact center to center. Source of truth for diff-drive
   # odometry (Phase 2) and velocity kinematics (Phase 4).
   TRACK_WIDTH_METERS = 0.__  # <-- fill in the measured value
   ```
   (Phase 3 includes an optional in-place-rotation cross-check to confirm the measured
   value against wheel odometry — a sanity check, not the way the number is obtained.)
3. Add a `SensorMount` frozen dataclass describing one sensor's pose in `base_link`,
   in meters and radians, REP-103:
   ```python
   @dataclass(frozen=True)
   class SensorMount:
       name: str            # matches the sensor `name` in sensors.json
       x: float             # forward, meters
       y: float             # left, meters
       z: float             # up, meters
       yaw: float = 0.0     # radians, CCW positive
       pitch: float = 0.0   # radians, positive tilts the forward axis downward
   ```
   Add a comment fixing the pitch sign convention (in a z-up frame a positive
   rotation about +y tilts the forward axis toward -z, so a downward-facing cliff
   sensor has *positive* pitch) and use it consistently.
4. Transcribe the sensor mounts into a `SENSOR_MOUNTS: tuple[SensorMount, ...]`,
   copying the REP-103 values Phase 0 wrote into the docs (mm→m only; **no sign flips
   — Phase 0 already put the docs in `base_link`**):
   - Forward ToFs from `docs/forward tof sensor geometry.md` "Mounting" section:
     left `y = +0.110`, right `y = -0.110`, center `y = 0.0`; side heights
     sides `z = 0.126`, center `z = 0.110`; forward inset `x = 0.055 - S`.
   - Downward cliff ToFs from `docs/robot dimensions.md`: `z = 0.067`,
     `y = ±0.128`, `x ≈ +0.064`, `pitch = +0.611` (35° down).
   - Use the same `name` strings that appear in the deployed `sensors.json` /
     `DEFAULT_SENSORS` (`cliff_left`, `cliff_center`, `cliff_right`, and the forward
     names) so a future TF publisher can join mounts to live readings by name.
5. In `src/robot_motion.py`, delete the three moved constants and import them from
   `robot_model` instead. Behavior is unchanged — this is a pure move. `grep` for
   every use of `WHEEL_DIAMETER_METERS`, `ENCODER_COUNTS_PER_WHEEL_REVOLUTION`, and
   `ENCODER_COUNTS_PER_METER` in `robot_motion.py` and repoint them.

Do **not** wire `SENSOR_MOUNTS` into any running service in this phase. It is
reference data that Phase 2+ and the URDF consume. Do not touch `sensors.json`
loading — mounts are fixed geometry, not runtime config.

### Tests

New `tests/test_robot_model.py`:
- `ENCODER_COUNTS_PER_METER` matches the hand-computed value (recompute from the two
  inputs, assert `math.isclose`).
- Every `SensorMount` in `SENSOR_MOUNTS` has a unique `name`.
- Spot-check the REP-103 values: `cliff` sensors have positive pitch; the right
  forward sensor has negative `y` and the left forward sensor positive `y` (guards
  against a left/right transcription slip).

### Acceptance

```bash
python3 -m unittest tests.test_robot_model tests.test_robot_motion
```

---

## Phase 2 — Dead-reckoning odometry as a pure, tested class

### Why

This is the single highest-value missing piece for SLAM. The pose-integration math
does not exist anywhere today. Build it as a pure class in the framework-agnostic
`control/` layer, fully unit-tested off-hardware, so the future ROS2 odom-publisher
node is a thin wrapper around it.

### Work

Create `src/control/odometry.py` with one small stateful class. Input is per-update
**signed wheel travel deltas in meters** (robot-forward positive), which is exactly
what `robot-motion` already computes per tick. No hardware, no sockets, no encoder
counts inside this module — counts→meters conversion stays at the call site using
`robot_model.ENCODER_COUNTS_PER_METER`.

```python
@dataclass
class Pose:
    x: float = 0.0       # meters, +forward from start
    y: float = 0.0       # meters, +left from start
    theta: float = 0.0   # radians, CCW positive, normalized to (-pi, pi]

class DiffDriveOdometry:
    def __init__(self, track_width_m: float): ...
    def update(self, left_delta_m: float, right_delta_m: float) -> Pose: ...
    def reset(self) -> None: ...   # back to (0, 0, 0)
```

Integration (exact-form midpoint; this is the standard diff-drive dead reckoning):

1. `d_center = (left_delta_m + right_delta_m) / 2`
2. `d_theta  = (right_delta_m - left_delta_m) / track_width_m`
   (right wheel ahead of left ⇒ CCW ⇒ positive theta — matches REP-103; comment this)
3. `theta_mid = self.theta + d_theta / 2`
4. `self.x += d_center * cos(theta_mid)`
5. `self.y += d_center * sin(theta_mid)`
6. `self.theta = normalize(self.theta + d_theta)` where `normalize` wraps to `(-pi, pi]`
7. return a copy of the current `Pose`

Keep it this small. No velocity estimate, no covariance, no IMU fusion here — a
ROS2 `robot_localization` EKF will fuse IMU yaw later; do not reimplement that now.

### Tests

New `tests/test_odometry.py`, all analytic (no hardware):
- Straight line: equal positive deltas ⇒ `x` increases by the delta, `y ≈ 0`,
  `theta ≈ 0`.
- Pure left turn in place: `left_delta = -d`, `right_delta = +d` ⇒ `theta` increases
  (positive), `x ≈ y ≈ 0`. Cross-check magnitude: `theta == 2*d / track_width`.
- Pure right turn: mirror, `theta` decreases.
- A quarter-circle arc (one wheel still, one moving) lands near the analytic
  endpoint within a tolerance that reflects the step count (document the tolerance).
- `theta` normalization: accumulate past `pi` and assert it wraps into `(-pi, pi]`.
- `reset()` returns to the origin.

### Acceptance

```bash
python3 -m unittest tests.test_odometry
```

---

## Phase 3 — Publish pose from robot-motion

### Why

Phase 2 is inert until something feeds it real wheel deltas and publishes the pose.
`robot-motion` already accumulates `self._left_distance_m` / `self._right_distance_m`
(`src/robot_motion.py:167-168`, `_accumulate_odometry` ~line 611). Feed the per-tick
deltas into `DiffDriveOdometry` and add the pose to the telemetry it already emits.

### Work

1. In `src/robot_motion.py`, construct one `DiffDriveOdometry(robot_model.TRACK_WIDTH_METERS)`
   alongside the existing distance counters.
2. In `_accumulate_odometry` (~line 611), you already compute the signed per-tick
   travel added to `_left_distance_m` / `_right_distance_m`. Pass those two per-tick
   deltas (meters) into `odometry.update(...)` in the same place. Keep the existing
   cumulative distance fields — they are still published and the dashboard diffs them.
3. When odometry is invalidated (`_invalidate_odometry_baseline`, ~line 602 — fires on
   encoder reset / restart), call `odometry.reset()` too, so pose restarts with the
   distance baseline. Verify: pose must not jump when the encoder baseline is
   re-established after a stop.
4. Extend `odometry_message` in `src/telemetry/messages.py:282` to carry the pose
   **as new keys**, leaving `left_distance_m` / `right_distance_m` exactly as they are
   (backward compatible — existing dashboard readers keep working):
   ```python
   def odometry_message(left_distance_m, right_distance_m,
                        x=None, y=None, theta=None): ...
   ```
   Populate `x`, `y`, `theta` from the current `Pose` at the `_odometry_payload`
   call site (~line 629).

Do not change the telemetry transport, the socket, or any consumer. This phase only
adds fields to an existing message.

### Tests

Extend `tests/test_robot_motion.py`:
- After a scripted straight-move tick sequence with fake encoder positions, the
  published `odometry` payload contains `x > 0`, `y ≈ 0`, `theta ≈ 0`.
- After a scripted in-place turn, `theta` moves in the expected direction.
- An encoder-baseline invalidation resets the pose (no jump).
- The legacy `left_distance_m` / `right_distance_m` keys are still present and
  unchanged.

### Acceptance

```bash
python3 -m unittest tests.test_robot_motion tests.test_telemetry_messages
```

### Optional cross-check (owner, on the robot)

`TRACK_WIDTH_METERS` comes from the physical measurement in Phase 1 — that is the
value to use. If you want to confirm it against wheel odometry:
1. Command a known in-place rotation (e.g. the existing `turn` intent, 360°).
2. Compare the published `theta` against the physical/IMU rotation.
3. A large mismatch means the measurement is off or there's meaningful wheel slip;
   re-measure first, then nudge the value only if a real scrub effect remains.

---

## Phase 4 — Velocity-native differential-drive kinematics

### Why

The drive path speaks `qpps`. ROS2 `/cmd_vel` is a `Twist` in m/s and rad/s, and the
node converts. Add that conversion now, in the framework-agnostic mixer, using the
Phase 1 constants. Then `/cmd_vel` becomes a drop-in later and the existing safety
gate / intents sit on top unchanged.

### Work

Extend `src/control/differential_drive.py` (do not replace the existing normalized
`mix` / `to_wheel_speeds` — gamepad teleop still uses them; add alongside):

1. `body_twist_to_wheel_qpps(linear_x_mps: float, angular_z_radps: float) -> WheelSpeedCommand`
   using standard diff-drive inverse kinematics with `TRACK_WIDTH_METERS` and
   `ENCODER_COUNTS_PER_METER` from `robot_model`:
   - `left_mps  = linear_x_mps - angular_z_radps * TRACK_WIDTH_METERS / 2`
   - `right_mps = linear_x_mps + angular_z_radps * TRACK_WIDTH_METERS / 2`
     (left-turn = positive angular_z ⇒ right wheel faster — matches Phase 2 sign; add
     an assertion-by-comment tying it to `DiffDriveOdometry`.)
   - convert each to `qpps` via `* ENCODER_COUNTS_PER_METER`, round to int.
2. `wheel_qpps_to_body_twist(left_qpps: int, right_qpps: int) -> MotionCommand`
   as the forward-kinematics inverse (for a future odom-from-commanded-velocity path
   and for tests): counts→m/s, then
   `linear_x = (left + right)/2`, `angular_z = (right - left)/TRACK_WIDTH_METERS`.
3. Keep both as free functions or `DifferentialDriveMixer` methods — whichever reads
   cleaner next to the existing code. No new class.

Do **not** rewire gamepad or robot-motion to use these yet. This phase adds the
math and its tests; adopting `/cmd_vel` is migration-time work. (This is the same
"build the seam, don't cross it yet" pattern as Phase 2.)

### Tests

Extend `tests/test_control.py`:
- Round-trip: `wheel_qpps_to_body_twist(body_twist_to_wheel_qpps(v, w))` recovers
  `(v, w)` within integer-rounding tolerance for several `(v, w)`.
- Pure rotation (`linear_x = 0`) gives equal-and-opposite wheel qpps.
- Pure translation (`angular_z = 0`) gives equal wheel qpps.
- Sign check: positive `angular_z` makes `right_qpps > left_qpps` (must agree with the
  Phase 2 odometry sign convention).

### Acceptance

```bash
python3 -m unittest tests.test_control
```

---

## Phase 5 — Separate the drive command from its telemetry passengers (owner-reviewed)

### Why

`DriveCommand` (`src/control/motion_drive.py:18`) smuggles a bag of telemetry
(`controller`, `wheels`, `drive_tuning`, `drive_status`, `link_loop`) alongside the
actual speed command. In ROS2, `/cmd_vel` is *just* a Twist and telemetry lives on
its own topics. Slimming this now makes the command path map to one clean topic.

**This phase is delicate — do not treat it as a mechanical refactor.** The passengers
carry a real, safety-relevant signal: `robot-motion` reads
`drive.drive_status["controller_reader_alive"]` (`src/robot_motion.py:418-420`) and
stops if the gamepad's controller reader died, and it *republishes* the gamepad
telemetry under its own source. Deleting the passengers would drop that behavior.

Recommendation: the owner scopes and reviews this one; a less-capable agent should
**not** land it autonomously. If in doubt, defer the whole phase to migration time —
Phases 1–4 deliver the SLAM-critical value on their own.

### Work (proposed direction, to be confirmed by owner before coding)

1. Audit every read of a `DriveCommand` passenger field in `src/robot_motion.py`
   (the grep: `controller` / `wheels` / `drive_tuning` / `drive_status` / `link_loop`
   on the `drive` object). Classify each as **command-critical** (must survive on the
   command path — e.g. `controller_reader_alive`, and whatever `_gamepad_active_from_wheels`
   needs) vs **telemetry-only** (republished for dashboards).
2. Define a lean `DriveCommand` = `left_qpps`, `right_qpps`, plus the minimal
   command-critical liveness fields, and move the telemetry-only passengers onto the
   telemetry hub as their own `gamepad_teleop` publish (gamepad already publishes a
   `gamepad_teleop_update` — fold the passengers there instead of through motion).
3. Update `gamepad_teleop.py` (producer) and `robot_motion.py` (consumer) together.
   Preserve the controller-reader-died stop exactly.

### Tests

- `tests/test_robot_motion.py`: motion still stops when the controller-reader-died
  signal arrives on the slimmed command.
- `tests/test_gamepad_teleop.py`: the telemetry-only fields still reach the hub.
- Round-trip `DriveCommand.to_message` / `from_message` for the new shape.

### Acceptance

```bash
python3 -m unittest tests.test_robot_motion tests.test_gamepad_teleop tests.test_telemetry_messages
```

---

## Phase 6 — Mark the topic-bound message shapes (light, documentation)

### Why

`telemetry/messages.py` is hand-rolled dicts. Most of it (voice, dashboard state) is
throwaway scaffolding and should stay as-is. But a handful map onto *real* ROS2
topics, and it helps future work to know which is which.

### Work

No behavior change. In `src/telemetry/messages.py`, add a short module-level comment
block, or a one-line comment above each of these functions, naming the ROS2 message
each is destined to become:

- `odometry_message` → `nav_msgs/Odometry` (+ TF `odom`→`base_link`)
- `wheel_message` → wheel/joint state
- `sensors_update` range readings → `sensor_msgs/Range` per sensor (joined to
  `robot_model.SENSOR_MOUNTS` by name for `frame_id`)
- `motor_battery_message` / `pi_battery_message` → `sensor_msgs/BatteryState`
- the drive command (Phase 5) → `geometry_msgs/Twist` on `/cmd_vel`

Explicitly note that `voice_update`, `vision_update`, and the dashboard hub payloads
are **scaffolding** and are expected to be replaced, not ported. Do not restructure
any payload here — this phase only records intent so the migration knows the map.

### Acceptance

Docs/comments only. Run the full baseline suite to confirm nothing was disturbed:

```bash
python3 -m unittest tests.test_telemetry_messages tests.test_robot_motion tests.test_robot_sensors
```

---

## Out of scope (this is prep, not the migration)

- Installing ROS2, writing nodes, launch files, or a URDF. Phase 1 assembles the URDF
  *inputs*; writing the URDF is migration work.
- The lidar driver. It is a brand-new driver (its readings become
  `sensor_msgs/LaserScan`, unlike the ToF cliff/forward sensors which become
  `Range`). Out of scope until ROS2 is in.
- IMU/odometry sensor fusion (an EKF). ROS2 `robot_localization` does this; Phase 2
  stays wheel-only on purpose.
- Rewiring gamepad or robot-motion onto `/cmd_vel` or velocity units (Phase 4 builds
  the math; adoption is migration work).

## Done criteria

- One module (`src/robot_model.py`) owns every physical constant and sensor mount
  pose, in REP-103 units, and `robot_motion.py` imports the drive-train constants from
  it instead of defining them.
- `DiffDriveOdometry` exists as a pure, tested class and `robot-motion` publishes
  `(x, y, theta)` in its odometry telemetry, backward-compatibly.
- Velocity ↔ wheel-qpps kinematics exist and round-trip in tests, sign-consistent with
  the odometry.
- The focused suite (including `tests.test_robot_model` and `tests.test_odometry`) is
  green.
- `TRACK_WIDTH_METERS` holds the real measured value (not a placeholder), and the
  geometry docs read in REP-103 with no "+x = right" language remaining.

## Style constraints (from CLAUDE.md)

Minimal, flat, no abstractions until the third use. Everything new here is pure
functions plus small frozen dataclasses in the layers that already survive migration
(`drivers/`, `control/`, and the new `robot_model`). No manager/service classes, no
dependency injection beyond the constructor arg `DiffDriveOdometry` already takes, no
speculative ROS shims. If a phase seems to want a new framework, stop and get
approval.
