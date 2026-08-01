# Motor and Control Code Review

Review of the motor and control stack (`drivers/motor.py`, `drivers/controller.py`,
`control/*`, `robot_motion.py`, `gamepad_teleop.py`, `config/drive_tuning.py`,
`robot_model.py`). No code changes — findings only.

Overall the stack is in good shape — safety gate, slew shaping, and encoder-move
watchdogs are thoughtful. Items below are ordered roughly by how much I'd care.

---

## Bugs

### 1. Voice/agent intents ignore the loaded drive tuning

`main()` loads the tuning config but only passes `qpps_slew_limit` into
`MotionConfig`. When an intent drives the wheels, a fresh default `DriveTuning()`
is constructed instead:

```python
# robot_motion.py — _target_from_drive_or_intent
tuning = DriveTuning()
intent_mixer = DifferentialDriveMixer(
    qpps=self.config.qpps,
    speed_scale=tuning.speed_scale,
    turbo_scale=tuning.turbo_scale,
)
```

So intents always run at the default `speed_scale=0.25` regardless of what's in
`drive_tuning.json`. This contradicts the comment in `motion_intent.py` ("a full
1.0 here means a move runs at the same normal-mode speed as a full forward stick
on the gamepad") — that's only true if the config file still has default values.
If you've tuned speed up or down, teleop and voice moves diverge silently.

### 2. `stop_reason` in `_run_motor_loop` is dead

Every failure path assigns a local `stop_reason` and breaks (lines 352, 361, 419,
426, 437), but after the loop it's never used — `self._set_drive_state("stopped")`
is called with no reason, so telemetry never reports *why* the motors stopped.
`gamepad_teleop._run_connected` has the same pattern and *does* use the reason
afterward, which suggests this was lost in a refactor. Also note the idle-release
break at line 372 exits with `stop_reason` never assigned — harmless only
because it's never read.

### 3. Two conflicting sign conventions for `angular_z` in the same package

The REP-103 helpers say positive angular_z is CCW (left turn):

```python
# differential_drive.py — body_twist_to_wheel_qpps
left_mps = linear_x_mps - angular_z_radps * TRACK_WIDTH_METERS / 2
right_mps = linear_x_mps + angular_z_radps * TRACK_WIDTH_METERS / 2
```

But the mixer that actually drives the robot uses the opposite sign — positive
angular_z speeds up the *left* wheel (clockwise / right turn):

```python
# differential_drive.py — DifferentialDriveMixer.mix
left = command.linear_x + command.angular_z
right = command.linear_x - command.angular_z
```

Everything downstream compensates (teleop stick-right is positive,
`_turn_angular_z` negates for left turns), so the robot behaves correctly today.
But `MotionCommand` is documented as "Twist-like," odometry's theta is
CCW-positive, and the ROS2 migration plan intends to publish twists from these
values. This is a sign-flip bug waiting to happen the moment the two paths meet.

### 4. A RoboClaw reconnect mid-intent replays the intent from scratch

On reconnect, `_wait_for_roboclaw` calls `intent_executor.reset_active_start()`,
and `_run_motor_loop` sets `self.encoder_move = None` on entry. For a move that
had already traveled 1.5 m of 2 m, the encoder baseline and target reset, so
the robot drives the *full* distance again — up to ~2× the requested travel.
Turns similarly lose accumulated yaw and re-turn the full angle.

### 5. A yaw-closed-loop turn can hang in the "checking" phase forever

`_turn_tick` waits for a fresh yaw sample (`fresh_yaw`) before deciding whether
to correct, and the no-progress watchdog is unreachable in that phase (it returns
early at line 333). If yaw samples freeze — e.g. the telemetry hub keeps replaying
an old snapshot where `stale` is still `False` — the wheels sit safely at zero,
but the executor stays active. Every later intent gets `"busy"` and the caller
gets `internal_timeout` after 35 s, until a stop request or gamepad input clears
it. The turning phase is watchdogged; the checking phase is not.

### 6. Button handling treats evdev autorepeat as release

In `controller.py`, `pressed = value == 1` — evdev EV_KEY delivers 0=release,
1=press, 2=repeat. A repeat event on RB would drop the dead-man and stop the
robot mid-drive. Fail-safe direction at least, but `value != 0` is the correct
check.

### 7. The fallback `ecodes` stub swaps BTN_WEST/BTN_NORTH

Real Linux codes are BTN_NORTH=307, BTN_WEST=308; the stub in `controller.py`
defines `BTN_WEST = 307, BTN_NORTH = 308`. Tests running without evdev exercise
a different X/Y button mapping than the hardware does. Teleop only uses
RB/LB/sticks, so nothing breaks today.

### 8. Turn progress is direction-blind

`_turn_tick` measures `magnitude = abs(turned_degrees)`, so if the robot gets
rotated the *wrong* way (bump, wheel slip on carpet), that rotation counts as
progress toward the target and the turn can "complete" facing the wrong direction.

---

## Races and protocol edges

- **`MotionIntentBridge._handle_connection` timeout can discard someone else's
  request.** On `done_event` timeout it sets `self._pending = None` — but its own
  request was already taken by the main loop, so if a *second* connection queued
  a request in the meantime, that one gets silently dropped (its handler then
  burns the full 35 s and reports `internal_timeout`). Also, after a timeout the
  intent keeps executing with nobody listening.
- **Client and server timeouts are equal.** `request_motion_intent` defaults to
  35 s, the same as the bridge's `INTENT_MAX_SECONDS`, so the client tends to
  time out right as the server replies. The client's should be longer.
- **`ControllerDriver.stop()` joins before the device is closed.** `read_loop()`
  blocks on the fd, so `_running = False` doesn't wake it and every clean stop
  eats the full 1 s join timeout. `cleanup()` closing the device *after* the
  join has it backwards — close first to unblock the reader, then join.

---

## Weird code / smells

- **Dead things.** `MotionRunner.__init__` creates `self.mixer` (line 138) which
  is never used anywhere. `gamepad_teleop.run_forever` wraps its loop in
  `try: ... finally: pass`.
- **Intent validation is duplicated** between `MotionIntentBridge._handle_connection`
  and `MotionIntentExecutor.start`, with subtle differences (the bridge accepts
  any finite `degrees`; the executor clamps to 1–360). Two validators for one
  protocol will drift.
- **`mix()` clamps after dividing by `scale = max(1.0, |left|, |right|)`** — the
  values are already guaranteed within ±1, so the clamp is dead.
- **`_read_next_telemetry_value` catches bare `Exception`**, so a
  *non*-recoverable serial fault during telemetry reads is downgraded to a
  warning, while the same fault on the command path deliberately crashes the
  service. Inconsistent by-design-or-accident.
- **In `robot_model.py`, the comment says `WHEEL_DIAMETER_METERS` is "the
  calibrated effective rolling diameter: 1.00 m commanded measured 0.97 m," but
  the value is exactly the nominal 0.096.** If the robot really travels 0.97 m
  per commanded meter, the effective diameter should be ~0.0931. Either the
  comment or the constant wasn't updated.

---

## Inefficiencies

- **Odometry integrates at ~1.25 Hz.** `_accumulate_odometry` only runs on every
  4th telemetry tick (0.2 s × 4 = 0.8 s per pose update). At the turn speed of
  ~36°/s that's ~29° of heading change per midpoint-integration step — coarse
  for dead reckoning. Ironically, during encoder moves the loop already reads
  positions every 50 ms tick and throws the data away for odometry purposes.
- **Serial traffic:** `read_wheel_speeds` issues two transactions (`ReadSpeedM1` +
  `ReadSpeedM2`) where RoboClaw supports a combined read; `set_speed`/`stop` in
  `motor.py` use two `DutyM1`/`DutyM2` packets (non-atomic — one can succeed and
  the other fail) when `DutyM1M2` exists. The main service uses the atomic
  `SpeedM1M2`, so this only affects the duty path and the test script.
- **`_target_from_drive_or_intent` builds a new `DriveTuning` and
  `DifferentialDriveMixer` every 50 ms tick**, and both services call `mix()`
  twice per tick (once directly, once inside `to_wheel_speeds`).
- **The loops sleep a fixed `loop_interval` after doing work**, so real loop rate
  is work + 50 ms, not 20 Hz — the `command_loop_hz` telemetry will always read
  low.
- **After idle release, the first gamepad drive command can wait up to
  `retry_interval` (1 s)** in `_wait_for_roboclaw`'s sleep before the reconnect
  handshake even starts — a noticeable stick-to-motion delay after every idle
  period.

---

## Improvements

- **Stick deadzones zero values below the threshold but don't rescale**, so output
  jumps from 0 straight to 0.15 — a rescale to `(value - dz) / (1 - dz)` would
  feel smoother.
- **`MotionDrivePublisher` never sets a socket timeout**; if robot-motion wedges
  while the kernel buffer fills, `sendall` blocks the teleop loop indefinitely
  (the RoboClaw's own serial timeout is the only backstop).
- **`DriveTuning.from_dict` clamps `turn_scale` to a max of 1.0**, but the CLI
  help calls it a "turn command multiplier" — you can only ever attenuate, never
  boost.
- **`ControllerState` is mutated by the reader thread and read by the main loop
  with no synchronization.** Individual attribute reads are safe in CPython, but
  a single loop tick can see a torn snapshot (e.g. new stick Y with old RB).
  Harmless today, worth knowing about.

---

## Suggested priority

| Priority | Item | Why |
|---|---|---|
| 1 | Intent tuning ignored (#1) | Voice moves silently diverge from teleop after config changes |
| 2 | Lost `stop_reason` (#2) | Field debugging is blind to why motors stopped |
| 3 | Intent replay after reconnect (#4) | Can double requested move distance or turn angle |
| 4 | `angular_z` sign convention (#3) | Will bite on ROS2 twist wiring |
| 5 | Turn "checking" hang (#5) | Can block all motion intents for 35 s |
| 6 | Intent bridge timeout race | Silent request drop under concurrent callers |
| 7 | Odometry integration rate | Coarse pose for dashboard / future nav |
