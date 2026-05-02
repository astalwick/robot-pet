# Boot-Ready Gamepad Teleop Plan

## Goal

Turn the working Xbox 360 controller + RoboClaw speed diagnostic into a boot-ready gamepad teleop service: when the robot is switched on, it waits safely for the controller and RoboClaw, then drives only while RB is held.

The reusable logic should stay framework-agnostic for a future ROS2 migration, but ROS2 readiness is a constraint, not the deliverable. The deliverable is a robot that is out of bench-test mode and ready to control with a gamepad after boot.

## Current State

- `scripts/diagnostics/controller-test.py` proves the Pi receives raw controller events.
- `scripts/diagnostics/controller-state.py` proves the raw events can become normalized controller state.
- `scripts/diagnostics/controller-roboclaw-test.py` proves controller input can drive RoboClaw at low duty with RB as deadman.
- `scripts/diagnostics/controller-roboclaw-speed-test.py` proves controller input can drive RoboClaw using closed-loop `SpeedM1M2` velocity commands with encoder feedback.
- `src/drivers/motor.py` owns direct RoboClaw access and now validates `ReadVersion`.
- `src/drivers/controller.py` already exists, but it should be reconciled with the mappings and behavior proven by the diagnostics.
- `scripts/teleop.py` is keyboard teleop and currently mixes input handling, teleop policy, UI, and motor output in one script.
- `systemd/robot-brain.service` already exists; the gamepad teleop path should integrate with the existing `systemd/` pattern rather than inventing a separate boot mechanism.

## Target Shape

```mermaid
flowchart LR
    Systemd["gamepad-teleop.service"] --> TeleopRunner["Gamepad Teleop Runner"]
    Gamepad["Xbox Controller"] --> ControllerDriver["ControllerDriver"]
    ControllerDriver --> ControllerState["ControllerState"]
    ControllerState --> TeleopPolicy["GamepadTeleopPolicy"]
    TeleopPolicy --> MotionCommand["MotionCommand linear_x angular_z"]
    MotionCommand --> DifferentialDrive["DifferentialDriveMixer"]
    DifferentialDrive --> WheelSpeedCommand["WheelSpeedCommand left_qpps right_qpps"]
    WheelSpeedCommand --> MotorDriver["MotorDriver RoboClaw SpeedM1M2"]

    MotionCommand -. "future ROS2 /cmd_vel" .-> Ros2Node["ROS2 Motion Node"]
```

The service is long-running and hardware-tolerant:

- If the controller is absent at boot, the service waits.
- If the controller disconnects or powers off, the service sends stop and waits for it to return.
- If the controller reconnects later, teleop becomes available again without restarting the robot.
- If the RoboClaw is absent or not responding, the service keeps motors stopped and retries until `ReadVersion` succeeds.

## Sign Conventions

Match the working behavior from `scripts/diagnostics/controller-roboclaw-speed-test.py`. That diagnostic is the hardware-proven source of truth:

- Forward/back input comes from `ABS_Y` on the left stick.
- Forward is `forward = -normalize_axis(ABS_Y)`.
- Turn input comes from `ABS_RX` on the right stick.
- Right turn is `turn = normalize_axis(ABS_RX)`.
- Arcade drive mixing is:
  - `left = clamp(forward + turn, -1.0, 1.0)`
  - `right = clamp(forward - turn, -1.0, 1.0)`
- `M1 = left`, `M2 = right`.
- Positive left and right QPPS together mean robot-forward.
- RB is the deadman switch.
- LB is turbo mode, matching the speed diagnostic.

Do not reinterpret these signs during the cleanup. If the robot drives correctly with the diagnostic, the boot-ready service should produce the same wheel targets for the same controller input.

## Implementation Plan

1. Normalize the controller driver

   Update `src/drivers/controller.py` using the mappings proven by `scripts/diagnostics/controller-test.py`:

   - Sticks: `ABS_X`, `ABS_Y`, `ABS_RX`, `ABS_RY`
   - Triggers: `ABS_Z`, `ABS_RZ`
   - D-pad: `ABS_HAT0X`, `ABS_HAT0Y`
   - Buttons from the observed Xbox 360 codes, including RB as deadman

   Keep this module pure Python + `evdev`, with no RoboClaw, ROS2, curses, or systemd knowledge.

2. Add reusable control primitives

   Add framework-agnostic modules:

   - `src/control/commands.py`
     - `MotionCommand(linear_x, angular_z)`
     - `WheelCommand(left, right)` as normalized wheel intent
     - `WheelSpeedCommand(left_qpps, right_qpps)` as RoboClaw closed-loop speed targets
   - `src/control/teleop.py`
     - `GamepadTeleopPolicy`
     - Converts `ControllerState` to `MotionCommand`
     - Applies deadman, deadzone, speed limit, optional speed modes
   - `src/control/differential_drive.py`
     - Converts `MotionCommand` to normalized left/right wheel commands
     - Converts normalized wheel commands to capped QPPS speed targets

   Important: teleop should produce a Twist-like command, not RoboClaw duty. The final motor output should preserve the proven closed-loop speed behavior from `scripts/diagnostics/controller-roboclaw-speed-test.py`, using `SpeedM1M2` with conservative QPPS caps rather than regressing to raw duty commands.

3. Promote closed-loop speed control into `MotorDriver`

   Extend `src/drivers/motor.py` with the higher-level RoboClaw speed operations proven by the diagnostic:

   - `set_wheel_speeds(left_qpps, right_qpps)` using `SpeedM1M2`
   - `read_wheel_speeds()` using `ReadSpeedM1` and `ReadSpeedM2`
   - `stop()` continuing to command zero output

   Keep the existing direct duty path only if diagnostics still need it. The normal driving path should use encoder/PID speed commands.

4. Add a boot-ready gamepad teleop runner

   Create a thin wiring script:

   ```text
   ControllerDriver -> GamepadTeleopPolicy -> DifferentialDriveMixer -> MotorDriver.set_wheel_speeds
   ```

   Behavior:

   - Runs forever until the service is stopped.
   - Starts safely even if the controller is not present yet.
   - Reconnects when the controller returns after powering off or disconnecting.
   - Waits for RoboClaw `ReadVersion` and an initial zero-speed command before enabling motion.
   - RB deadman required for motion.
   - Default QPPS speed cap starts conservative, e.g. `--speed-scale 0.25` of configured QPPS.
   - Optional turbo mode can reuse the proven LB behavior from the speed diagnostic.
   - CLI flags allow `--qpps`, `--speed-scale`, `--turbo-scale`, `--deadzone`, `--port`, `--baud`, and `--address`.
   - Releasing RB sends stop.
   - Ctrl+C sends stop and cleans up.
   - Controller disconnect sends stop, logs clearly, and returns to waiting for a controller.

   This should live as `src/gamepad_teleop.py`. The underscore keeps it importable for tests while still letting systemd run it directly. Keep `scripts/` for manual commands and diagnostics.

5. Add `systemd/gamepad-teleop.service`

   Add a dedicated systemd service alongside `systemd/robot-brain.service`:

   - `After=robot-brain.service`
   - `Wants=robot-brain.service`
   - `WorkingDirectory=/home/pi/robot-pet/src`
   - `Environment=PYTHONPATH=/home/pi/robot-pet/src`
   - `ExecStart=/home/pi/robot-pet/.venv/bin/python /home/pi/robot-pet/src/gamepad_teleop.py`
   - `Restart=always`
   - `RestartSec=2`
   - `WantedBy=multi-user.target`

   Keep this separate from `robot-brain.py`. A teleop crash should restart independently, logs should be easy to inspect with `journalctl -u gamepad-teleop -f`, and the service can be deleted cleanly when ROS2 launch files replace systemd wrappers later.

6. Keep diagnostics separate

   Leave `scripts/diagnostics/` as hardware bring-up tools:

   - Raw controller event inspection
   - Normalized controller state inspection
   - Low-duty controller-to-RoboClaw test
   - Closed-loop speed controller-to-RoboClaw test
   - RoboClaw version/command tests

   These scripts remain useful even after full teleop exists.

7. Update setup and docs

   `setup.sh` already installs `evdev` and adds the user to `input`; verify this is enough for the final teleop path.

   `setup.sh` already copies all `systemd/*.service`; update it to enable and restart `gamepad-teleop.service` explicitly alongside `robot-brain.service`.

   Update docs with:

   - How to run diagnostics
   - How to run the gamepad service
   - How to inspect logs with `journalctl -u gamepad-teleop -f`
   - How to run a one-off foreground gamepad teleop process for debugging
   - Safety checklist: wheels up first, low speed first, RB deadman, RoboClaw powered and version-readable
   - ROS2 migration note: `MotionCommand` maps directly to future `geometry_msgs/Twist`
   - Keyboard teleop is legacy/manual fallback only; normal driving is gamepad or automation.

## Safety Rules

- No motor command unless the RoboClaw responds to `ReadVersion`.
- No motion unless an initial zero-speed command has succeeded.
- No motion unless RB deadman is held.
- Stop on deadman release.
- Stop on Ctrl+C or normal exit.
- Stop on controller disconnect.
- Continue running and wait for controller reconnect after disconnect.
- Clamp all commands before reaching `MotorDriver`.
- Use closed-loop speed commands with conservative default QPPS caps for normal driving.
- Keep low default speed for the first boot-ready service.

## Failure Behavior

The long-running service should treat controller and RoboClaw availability as runtime state, not as one-shot startup checks:

- If no controller is present, log that teleop is waiting and retry until one appears.
- If controller reads fail after connection, send stop, close the input device, clear controller state, and return to the controller wait loop.
- If `ReadVersion` fails, keep motors stopped and retry RoboClaw readiness.
- If the initial `SpeedM1M2(0, 0)` is not acknowledged, do not enable motion; log clearly and retry RoboClaw readiness.
- If a later `SpeedM1M2(left_qpps, right_qpps)` command is not acknowledged, treat the RoboClaw as unavailable, attempt one zero-speed command if the serial connection still exists, close/recreate the driver, and return to the RoboClaw wait loop.
- If shutdown or Ctrl+C happens while RoboClaw is ready, send zero QPPS before closing the driver.
- Systemd restart is a backstop for unexpected crashes, not the normal recovery mechanism for unplugged hardware.

## Test Plan

Add focused stdlib `unittest` coverage for the framework-agnostic pieces and runner behavior that can be tested without real hardware:

- `GamepadTeleopPolicy`
  - RB released always returns zero motion.
  - RB held converts left-stick Y and right-stick X into `MotionCommand`.
  - Deadzone zeros small stick noise.
  - LB turbo selects the turbo scale only while held.
- `DifferentialDriveMixer`
  - Uses the proven arcade mix: `left = forward + turn`, `right = forward - turn`.
  - Clamps normalized wheel commands to `[-1.0, 1.0]`.
  - Converts normalized wheels to capped QPPS without exceeding the configured cap.
  - Preserves the diagnostic sign convention where positive left and right targets mean robot-forward.
- `MotorDriver`
  - `set_wheel_speeds(left_qpps, right_qpps)` calls RoboClaw `SpeedM1M2(address, left_qpps, right_qpps)`.
  - `read_wheel_speeds()` reads `ReadSpeedM1` and `ReadSpeedM2`.
  - `stop()` sends zero output; if the normal driving path uses speed mode, zero-speed behavior should be covered.
- `gamepad_teleop` runner
  - Starts with no motion until controller and RoboClaw readiness both succeed.
  - Sends stop on deadman release.
  - Sends stop and returns to waiting on controller disconnect.
  - Treats failed speed-command acknowledgement as RoboClaw readiness loss.
  - Sends stop on shutdown.

## ROS2 Migration Path

When moving to ROS2:

- Keep `src/drivers/motor.py` as the hardware backend.
- Keep `src/drivers/controller.py` if the Pi still reads the gamepad directly.
- Keep `src/control/teleop.py` and `src/control/differential_drive.py`.
- Replace `src/gamepad_teleop.py` and `systemd/gamepad-teleop.service` with ROS2 nodes/launch files:
  - A teleop node publishes `MotionCommand` as `geometry_msgs/Twist` on `/cmd_vel`.
  - A motion node subscribes to `/cmd_vel`, mixes to wheel speed commands, and calls `MotorDriver.set_wheel_speeds`.

The desired migration is transport-level: Python service loop today, ROS2 topic loop later.

## Suggested First Cut

Implement the reusable pieces first, promote the proven `SpeedM1M2` behavior into `MotorDriver`, then make the boot service small. The diagnostics have already proven the hardware path, so the next risk is turning it into a safe long-running service without adding unnecessary framework machinery.
