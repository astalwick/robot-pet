# RoboClaw Encoder Distance Decisions

This doc captures the fixed semantic decisions for using RoboClaw encoder positions to make robot motion distances more accurate.

## Summary

The goal is to replace the current time-based forward distance estimate with encoder-position feedback from the RoboClaw. The user-facing `move` tool should continue to accept meters, but `robot-motion` should be able to stop based on measured wheel encoder travel instead of "seconds at a calibrated speed." This should reduce run-to-run distance variance for straight moves while preserving existing safety behavior, gamepad preemption, and the simple motion-intent architecture.

## How Motion Works Today

Voice exposes a `move` tool with a `distance_meters` argument in `src/voice/assistant.py`. The shared tool dispatcher in `src/voice/tools.py` currently converts that distance to `duration_seconds` using `MOVE_METERS_PER_SECOND` from `src/control/motion_intent.py`, then sends the request to `robot-motion` over `/run/robot-pet/motion-intent.sock`.

`robot-motion` receives only a `move` intent with optional signed `duration_seconds`. `MotionIntentExecutor` is a pure time-based state machine. During a move, it emits a constant `MotionCommand(linear_x=+/-0.3, angular_z=0.0)` until elapsed time reaches the requested duration. The executor has no access to hardware readings.

`robot-motion.py` owns RoboClaw access. It accepts motion intents, waits for the RoboClaw to become ready, resets the active intent start time once the initial zero-speed command succeeds, then runs the motor loop. The motor loop converts either gamepad drive packets or motion intents into left/right QPPS targets and calls `MotorDriver.set_wheel_speeds(...)`. A recent fix makes voice motion intents use fixed default intent tuning instead of inheriting gamepad speed/turbo tuning.

The gamepad always wins. If a non-zero gamepad command is active during a motion intent, `MotionIntentExecutor.tick(..., gamepad_active=True)` preempts the intent and reports `preempted_by_gamepad`. Stop requests also cancel the active intent through a fast path.

The current RoboClaw driver already uses encoder-backed closed-loop speed commands:

- `MotorDriver.set_wheel_speeds(left_qpps, right_qpps)` calls RoboClaw `SpeedM1M2(...)`.
- `MotorDriver.read_wheel_speeds()` calls `ReadSpeedM1(...)` and `ReadSpeedM2(...)`.
- `MotorDriver.read_max_qpps()` reads configured velocity PID caps.
- `robot-motion` telemetry publishes target QPPS, actual encoder QPPS, error, current, and read status.

The driver does not currently expose encoder positions. There is no wrapper for RoboClaw encoder count reads such as `ReadEncM1(...)` / `ReadEncM2(...)`, and no motion-intent state tracks starting encoder counts or target encoder deltas.

The latest settled motor spec is the goBILDA 5203-2402-0019 output-shaft encoder value: 537.7 counts per output-shaft revolution. With 96 mm directly mounted wheels, that is roughly 1,783 counts per meter. This is enough resolution for short-distance motion, but encoder odometry measures wheel rotation, not actual ground travel. Wheel slip and floor conditions can still cause error.

## Current Constraints And Surprises

- The public tool should stay meter-based. The user should not have to think in seconds or encoder counts.
- The socket API currently accepts `duration_seconds`, not `distance_meters`, for `move`.
- Distance conversion currently happens in `robot-voice`, not `robot-motion`, so calibration constants in `src/control/motion_intent.py` require restarting `robot-voice`.
- `robot-motion` already has the hardware object needed to read encoders; `MotionIntentExecutor` intentionally does not.
- The motor loop currently samples optional telemetry on a slower cadence to avoid delaying the command heartbeat.
- Encoder position reads needed for distance completion would be part of the control loop, not optional dashboard telemetry.
- Safety gating already cancels unsafe forward QPPS while preserving rotation and reverse.
- There is a known immediate-forward-safety boundary: if a forward safety rule trips, forward QPPS is cancelled before commands reach RoboClaw.
- The existing timed move maximum is 30 seconds and the motion-intent socket reply timeout is `MOVE_MAX_DURATION + 5`.

## CPR Read Research

The installed `basicmicro` Python API exposes encoder position reads:

- `ReadEncM1(address)` returns success, motor 1 encoder count, and status.
- `ReadEncM2(address)` returns success, motor 2 encoder count, and status.
- `GetEncoders(address)` returns success and both encoder counts.

It also exposes encoder mode and PID/config reads:

- `ReadEncoderModes(address)` returns encoder mode bytes.
- `ReadM1VelocityPID(address)` and `ReadM2VelocityPID(address)` return velocity PID values and QPPS.
- `ReadM1PositionPID(address)` and `ReadM2PositionPID(address)` return position PID values and min/max position limits.
- `GetConfig(address)` returns the RoboClaw config word.

I do not see a `ReadCPR`, `ReadPPR`, or equivalent encoder-counts-per-revolution read in the installed library. The current `scripts/diagnostics/roboclaw-read-config.py` also only reads encoder modes, config, PID/QPPS, current limits, voltage limits, and status. An older unused setup script explicitly notes that the library may not have a direct encoder CPR function.

So the clean V1 read path appears to be: read live encoder positions from RoboClaw, but keep the motor encoder CPR and wheel diameter as robot configuration unless we find a separate RoboClaw command outside the installed library.

## Encoder Move Control Boundary

### Question

Should encoder-distance moves stay under `robot-motion` control as "command wheel speed, read encoder positions each loop, stop when target counts are reached," rather than using RoboClaw's built-in distance or position commands if available?

### Decision

Encoder-distance moves will stay under `robot-motion` control. `robot-motion` will command normal closed-loop wheel speed targets, read RoboClaw encoder positions during the motor loop, and stop the motion intent when the target encoder delta has been reached.

This keeps the move inside the same safety and lifecycle boundary as current voice motion intents. The existing safety gate can still cancel unsafe forward QPPS, stop requests can still cancel the active intent through the fast path, and gamepad input can still preempt autonomous motion. RoboClaw built-in position or distance commands may exist, but using them as the primary move primitive would make cancellation, safety gating, and telemetry harder to reason about in the current architecture.

## Distance Contract Ownership

### Question

Should the `move` tool/socket contract change so `robot-voice` sends `distance_meters` to `robot-motion`, and `robot-motion` owns all meter-to-encoder conversion?

### Decision

Yes. `robot-voice` and any future caller should send the user-facing move distance as meters. `robot-motion` will own the conversion from meters to encoder counts and all encoder-position handling.

This removes distance implementation details from voice and prevents the restart/deploy split where a control calibration change can require a `robot-voice` restart. Encoder positions are a motion-controller concern. Other services should not need to know about encoder counts, counts-per-meter, starting encoder snapshots, or completion thresholds.

## Signed Distance Semantics

### Question

For encoder-based `move`, should reverse movement be supported with the same signed `distance_meters` contract, or should V1 only support forward encoder distance and keep reverse as timed/manual?

### Decision

Encoder-based `move` will support both forward and reverse using the existing signed distance semantics. Positive `distance_meters` means forward. Negative `distance_meters` means reverse.

This preserves the current public tool contract and keeps the implementation symmetric for straight-line distance moves. The encoder completion logic should use the magnitude of average wheel encoder delta to decide when the requested distance has been reached, while the sign determines the commanded direction.

## Encoder Read Failure Behavior

### Question

When encoder reads fail during an encoder-distance move, should the robot stop and return a tool failure instead of falling back to timed movement?

### Decision

Encoder read failure during an encoder-distance move will stop the robot and return a tool failure. The move should not fall back to timed movement.

The purpose of this feature is to avoid unreliable timed distance estimates. Falling back to timing would hide the encoder failure and could let the robot travel farther or shorter than requested without making the failure visible to the caller. A failed encoder read should therefore end the intent with an error result, leave the motor command at zero, and surface the reason through the normal motion-intent result path.

## Safety Block Behavior

### Question

What should happen if safety gating blocks forward motion during an encoder-distance move: should it stop/fail the intent immediately, or keep the intent active while commanding zero until the path clears?

### Decision

If safety gating blocks forward motion during an encoder-distance move, the robot will stop and fail the intent immediately. The intent will not remain active while commanding zero and waiting for the path to clear.

An encoder-distance move is an autonomous physical action. If the safety gate removes forward QPPS, the robot is no longer making progress toward the requested distance. Keeping the intent armed would make behavior less predictable and could resume movement later without a fresh user request. The result should surface the safety failure through the normal tool result path.

## Counts Per Meter Source

### Question

Should encoder-distance moves use a configurable counts-per-meter constant in code for V1, or read/derive it from wheel diameter and motor configuration?

### Decision

Encoder-distance moves should derive counts per meter from wheel diameter and motor/encoder configuration rather than using a single opaque counts-per-meter constant.

This keeps the distance model tied to physical robot facts: wheel circumference and encoder counts per wheel revolution. It also makes future recalibration easier to reason about because changing wheel diameter, encoder CPR, or gear/motor configuration updates the derived distance scale directly instead of requiring a hand-tuned aggregate value.

## Motor And Wheel Constants

### Question

Should the motor encoder CPR be read from RoboClaw, or should it be a constant for the installed motor SKU?

### Decision

The motor encoder CPR will be a constant for the installed drivetrain motor SKU. The drivetrain motors are goBILDA 5203-2402-0019 Yellow Jacket motors, 312 RPM, with built-in encoders. Use `537.7` counts per output-shaft revolution as the motor/wheel encoder count, assuming the 96 mm goBILDA Hogback wheels are mounted directly on the motor output shaft.

Distance conversion should derive counts per meter from:

- motor encoder counts per output-shaft revolution: `537.7`
- wheel diameter: `0.096` meters
- wheel circumference: `pi * wheel_diameter_meters`

The code should use clear named constants for these physical facts, then derive counts per meter from them.

## Distance Completion Rule

### Question

For a straight encoder-distance move, should completion use the average absolute travel of both wheels, or require both wheels to individually reach the target count delta before stopping?

### Decision

Completion should use the average absolute travel of both wheels.

This matches center-of-robot travel better for a straight differential-drive move and avoids overdriving one side if the two wheels differ slightly. Each loop should read both encoder positions, compute each wheel's absolute delta from the starting encoder snapshot, average those two deltas, and stop when that average reaches the target count delta.

## Approach Speed

### Question

Should encoder-distance moves slow down as they approach the target, or keep the current simple behavior of commanding normal move speed until average encoder travel reaches the target and then stopping?

### Decision

V1 will stay simple: command the normal move speed until average encoder travel reaches the target, then stop. It will not add a final approach speed, ramp, or proportional slowdown yet.

Overshoot tuning can be layered on later if the simple encoder threshold is not accurate enough. The first encoder implementation should prove the basic read-counts-and-stop loop without adding extra control behavior.

## Encoder Snapshot

### Question

Should a move reset the RoboClaw encoder counts at the start, or read the current encoder counts and use deltas from that snapshot?

### Decision

Use snapshot-and-delta. At the start of an encoder-distance move, read the current left and right encoder counts and treat those as the start positions. During the move, compute distance from the difference between the current encoder counts and that starting snapshot.

The implementation should not reset RoboClaw encoder counts. Resetting would make this feature more invasive and could surprise telemetry, diagnostics, or future odometry that also expect encoder positions to be continuous.

## Move Socket Contract

### Question

Should the internal motion-intent socket/API accept only `distance_meters` for `move` after this change, or support both `distance_meters` and legacy `duration_seconds` during a transition?

### Decision

The `move` contract should use `distance_meters` only. Remove `duration_seconds` from move requests instead of keeping it as a legacy path.

Distance is the only meaningful move input. The public tool already uses meters, and `robot-motion` should own the meter-to-encoder conversion. Keeping `duration_seconds` around would preserve the old time-based path and make it easier for voice, tests, or future callers to accidentally bypass encoder distance control.

## No-Progress Watchdog

### Question

Should encoder moves have a runtime/progress watchdog that stops and fails if encoder travel is not increasing, or should they rely only on encoder read failures, stop requests, safety, and gamepad preemption?

### Decision

Encoder moves should include a simple no-progress watchdog.

This is a real concern because the existing socket reply timeout does not cancel an active intent once `robot-motion` has taken it. If encoder reads continue to succeed but the counts do not advance, the motor loop could keep commanding motion until some outside stop, safety event, or gamepad preemption. The watchdog should be small and direct: while an encoder-distance move is commanding non-zero wheel speed, if average encoder travel does not increase for a short timeout, stop the robot and fail the intent.

## Maximum Move Distance

### Question

Should `move` keep a maximum allowed distance now that `duration_seconds` is going away?

### Decision

Yes. The maximum requested move distance is `2.0` meters.

`move` is still a voice-callable autonomous physical action, so it should remain bounded even though completion is encoder-based. Distances larger than 2 meters should be clamped to 2 meters, preserving the requested direction.
