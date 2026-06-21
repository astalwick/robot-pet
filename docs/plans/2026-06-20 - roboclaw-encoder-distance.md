# RoboClaw Encoder Distance Plan

Goal: move `move` completely off time-based distance estimates and onto RoboClaw
encoder position feedback. The public tool remains meter-based, and
`robot-motion` owns all encoder reads, meter-to-count conversion, completion,
and failure behavior.

Decision source:

- `docs/plans/2026-06-20 - roboclaw-encoder-distance.decisions.md`

Follow `AGENTS.md`: keep this direct, avoid new layers, and prefer small focused
state in `robot-motion` over a broad motion-control rewrite.

## Baseline

Run the focused tests before changing behavior:

```bash
python3 -m unittest tests.test_motor tests.test_motion_intent tests.test_robot_motion tests.test_voice_core tests.test_voice_agent_runner
```

Keep these green after each phase. If a phase only touches one area, run that
focused test first, then run the full command above before handoff.

## Phase 1 - Add Encoder Position Reads To MotorDriver

### Why

`robot-motion` owns RoboClaw access, but `MotorDriver` does not expose encoder
positions yet. The installed `basicmicro` API has `GetEncoders(address)`, which
returns both encoder counts in one call.

### Work

In `src/drivers/motor.py`:

1. Add `read_wheel_positions()` returning `(left_count, right_count)`.
2. Call `self.controller.GetEncoders(self.address)`.
3. Return `(None, None)` if the read is not acknowledged.
4. On recoverable RoboClaw exceptions, log a warning and return `(None, None)`.
5. Re-raise unexpected exceptions.

Keep this as a plain driver method. Do not add an encoder wrapper class.

### Tests

In `tests/test_motor.py`:

- Add `GetEncoders` to `FakeRoboClaw`.
- Assert `read_wheel_positions()` returns both counts.
- Assert recoverable timeout returns `(None, None)`.
- Assert an unexpected exception still propagates.

### Acceptance

```bash
python3 -m unittest tests.test_motor
```

## Phase 2 - Make Move Requests Distance-Only

### Why

The public tool already accepts `distance_meters`, but `src/voice/tools.py`
currently converts meters to `duration_seconds` before sending the socket
request. That preserves the old time-based path.

### Work

In `src/control/motion_intent.py`:

1. Replace move `duration_seconds` handling with `distance_meters`.
2. Validate that `distance_meters` is a finite real number, not a bool.
3. Require non-zero signed distance.
4. Clamp distance magnitude to at most `2.0` meters while preserving direction.
5. Remove move clamping by seconds.
6. Remove `MOVE_METERS_PER_SECOND`, `MOVE_MIN_DURATION`, and `MOVE_MAX_DURATION`
   if no remaining code needs them.
7. Keep `duration_seconds` for `diagnostic_turn`; this plan only removes it
   from `move`.

In `src/voice/tools.py`:

1. Stop importing `MOVE_METERS_PER_SECOND`.
2. Validate the public `distance_meters` argument as it does today.
3. Send `distance_meters` through to `motion_intent_caller`.

In tests:

- Update voice and agent-runner tests to assert `distance_meters` is passed
  through.
- Update motion-intent socket tests so `move` accepts `distance_meters` and
  rejects `duration_seconds`-only requests.
- Update invalid-distance tests for bool, string, NaN, infinity, and zero.
- Add tests that distances over 2 meters are clamped to 2 meters.

### Acceptance

```bash
python3 -m unittest tests.test_motion_intent tests.test_voice_core tests.test_voice_agent_runner
```

## Phase 3 - Keep MotionIntentExecutor Pure

### Why

`MotionIntentExecutor` is useful because it has no hardware IO. Encoder reads
belong in `robot-motion`, where the RoboClaw object and safety gate already
live.

### Work

In `src/control/motion_intent.py`:

1. Store signed `distance_meters` on active move intents.
2. For `move`, keep returning `MotionCommand(linear_x=+/-MOVE_LINEAR_X,
   angular_z=0.0)` while the intent is active.
3. Do not complete move intents from elapsed time.
4. Add a small method or property so `robot-motion` can read the active move
   distance without reaching into private state.
5. Keep gamepad preemption behavior unchanged.
6. Keep timed behavior for `express`, `diagnostic_turn`, `face_me`, and `turn`.

Do not move encoder math into this module.

### Tests

In `tests/test_motion_intent.py`:

- Move starts with positive and negative `distance_meters`.
- Move keeps producing the correct forward/reverse command across ticks.
- Move does not finish because time elapsed.
- Gamepad still preempts move.
- Invalid distances are rejected.

### Acceptance

```bash
python3 -m unittest tests.test_motion_intent
```

## Phase 4 - Add Encoder Distance State In robot-motion

### Why

`robot-motion` is the right boundary for encoder completion because it owns the
motor loop, RoboClaw driver, safety checks, stop handling, and gamepad
preemption.

### Work

In `src/robot_motion.py`:

1. Add named physical constants near the motion constants:
   - `WHEEL_DIAMETER_METERS = 0.096`
   - `ENCODER_COUNTS_PER_WHEEL_REVOLUTION = 537.7`
   - derived `ENCODER_COUNTS_PER_METER`
   - `MOVE_MAX_DISTANCE_METERS = 2.0`
2. When a move intent first becomes active, read a starting encoder snapshot
   with `motor.read_wheel_positions()`.
3. If the start read fails, stop/fail the intent with an encoder read error.
4. Each motor loop while move is active, read current encoder positions.
5. Compute average absolute wheel travel:
   - `abs(left_current - left_start)`
   - `abs(right_current - right_start)`
   - average the two
6. Stop and complete the intent when the average reaches the target count delta.
7. Do not reset RoboClaw encoders.
8. Do not add final approach speed or ramping in this pass.
9. Clear encoder-move state on completion, failure, stop, gamepad preemption,
   shutdown, or service restart.

Keep the state local and readable. A small dataclass is fine if it makes the
active encoder move fields obvious; do not introduce a manager/service layer.

### Tests

In `tests/test_robot_motion.py`:

- A move snapshots starting encoder positions before commanding motion.
- A positive distance completes when average absolute delta reaches target.
- A negative distance commands reverse and completes from absolute delta.
- Failed start encoder read fails the pending intent and sends zero speed.
- Failed mid-move encoder read fails the pending intent and sends zero speed.
- Stop request clears encoder move state.
- Gamepad preemption clears encoder move state.

### Acceptance

```bash
python3 -m unittest tests.test_robot_motion
```

## Phase 5 - Fail Encoder Moves When Safety Cancels Forward Progress

### Why

The safety gate can remove forward QPPS before commands reach RoboClaw. For an
encoder-distance move, that means the robot is no longer executing the requested
action and should not stay armed waiting to resume later.

### Work

In `src/robot_motion.py`:

1. Detect when an active encoder move is commanding forward motion and the safety
   gate blocks it.
2. Send zero speed.
3. Fail the pending intent with a clear reason such as `safety_blocked`.
4. Preserve current reverse and rotation behavior outside encoder moves.

### Tests

In `tests/test_robot_motion.py`:

- Forward encoder move fails when safety blocks forward QPPS.
- Reverse encoder move is not failed by forward-only safety blocking.
- The failure response includes a stable error string.

### Acceptance

```bash
python3 -m unittest tests.test_robot_motion
```

## Phase 6 - Add A Simple No-Progress Watchdog

### Why

The socket/client timeout does not cancel an active intent once `robot-motion`
has taken it. If encoder reads keep succeeding but counts do not advance, the
motor loop could keep commanding motion indefinitely.

### Work

In `src/robot_motion.py`:

1. Track the last average encoder travel and when it last increased.
2. While an encoder move is commanding non-zero wheel speed, fail if average
   travel has not increased for a short timeout.
3. Keep the timeout as one named constant.
4. On watchdog failure, send zero speed and fail the pending intent with a clear
   reason such as `encoder_no_progress`.

This should be a simple stuck-move guard, not a full velocity estimator.

### Tests

In `tests/test_robot_motion.py`:

- If encoder counts do not change while motion is commanded, the watchdog stops
  and fails the intent.
- If counts increase before the timeout, the watchdog does not fire.

### Acceptance

```bash
python3 -m unittest tests.test_robot_motion
```

## Phase 7 - Remove Timed-Move Calibration Leftovers

### Why

After encoder moves are in place, stale time-distance constants and comments
will mislead future calibration work.

### Work

Remove or rewrite move-specific time calibration artifacts:

- `MOVE_METERS_PER_SECOND`
- move uses of `duration_seconds`
- move tests that assert elapsed-time completion
- user-facing or internal comments that describe move distance as time-based

Keep time-based constants that still belong to `diagnostic_turn`, `face_me`,
`turn`, and expressions.

### Tests

Run the full focused suite:

```bash
python3 -m unittest tests.test_motor tests.test_motion_intent tests.test_robot_motion tests.test_voice_core tests.test_voice_agent_runner
```

## Phase 8 - Robot Smoke Test

### Why

Encoder math can be unit-tested, but wiring, motor direction, and encoder sign
need hardware confirmation.

### Work

On the robot:

1. Deploy `src/control`, `src/drivers`, `src/voice`, and `src/robot_motion.py`.
2. Restart `robot-motion.service` and `robot-voice.service`.
3. Ask for a small move, such as `0.25` meters.
4. Confirm it moves roughly the requested distance and stops.
5. Ask for `-0.25` meters and confirm reverse works.
6. Ask for `1.0` meter and measure actual travel.
7. Trigger stop during a move and confirm it cancels immediately.
8. Confirm a move over `2.0` meters is clamped to 2 meters.

If measured distance is consistently off, adjust the physical constants only
after confirming the motor SKU, wheel diameter, and direct wheel mounting are
still true.

## Done Criteria

- `move` accepts and sends `distance_meters` all the way to `robot-motion`.
- No move path converts meters to seconds.
- `robot-motion` stops moves using RoboClaw encoder position deltas.
- Encoder read failure, safety block, stop, gamepad preemption, and no-progress
  watchdog all stop/fail clearly.
- The focused unit tests pass.
- A robot smoke test confirms forward and reverse distance moves.
