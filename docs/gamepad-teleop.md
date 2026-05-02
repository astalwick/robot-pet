# Gamepad Teleop

Boot-ready gamepad teleop runs as `gamepad-teleop.service`. It waits for the Xbox 360 controller and RoboClaw, commands an initial zero speed, then drives only while RB is held.

## Safety Checklist

- Put the wheels up for first tests after any wiring, RoboClaw config, or sign-convention change.
- Start with the default `--speed-scale 0.25`.
- Keep RB released until you are ready for motion.
- Confirm the RoboClaw is powered and `ReadVersion` works.
- Release RB, unplug the controller, or stop the service to command zero speed.

## Diagnostics

Run diagnostics from the repo root with the venv active:

```bash
source .venv/bin/activate
python scripts/diagnostics/controller-test.py
python scripts/diagnostics/controller-state.py
python scripts/diagnostics/roboclaw-read-version.py
python scripts/diagnostics/controller-roboclaw-speed-test.py --speed-scale 0.15
```

`controller-roboclaw-speed-test.py` is the hardware-proven source of truth for gamepad signs and wheel targets.

## Service

Install and enable services with:

```bash
./setup.sh
```

Inspect the gamepad service with:

```bash
sudo systemctl status gamepad-teleop
journalctl -u gamepad-teleop -f
```

The service publishes read-only dashboard telemetry to `robot-telemetry.service` at 5 Hz. Telemetry is best-effort; if the hub is stopped or restarting, driving continues.

Restart it after changing code:

```bash
sudo systemctl restart gamepad-teleop
```

## Foreground Debugging

Stop the service before running a foreground teleop process so only one process owns the RoboClaw:

```bash
sudo systemctl stop gamepad-teleop
cd ~/robot-pet
source .venv/bin/activate
PYTHONPATH=src python src/gamepad_teleop.py --speed-scale 0.15
```

Useful flags:

- `--device /dev/input/eventX`
- `--port /dev/serial0`
- `--address 0x80`
- `--baud 38400`
- `--qpps 2425`
- `--speed-scale 0.25`
- `--turbo-scale 0.75`
- `--deadzone 0.15`

## Controls

- Left stick Y: forward/back. Forward is `-ABS_Y`.
- Right stick X: turn. Right turn is `ABS_RX`.
- RB: deadman. Motion is zero unless RB is held.
- LB: turbo mode while held.

The mixer matches the working speed diagnostic:

```text
left = clamp(forward + turn, -1.0, 1.0)
right = clamp(forward - turn, -1.0, 1.0)
M1 = left
M2 = right
positive left and right QPPS = robot-forward
```

## ROS2 Note

`MotionCommand(linear_x, angular_z)` is intentionally Twist-shaped. When ROS2 is introduced, the transport changes to `/cmd_vel`; the controller policy, mixer, and `MotorDriver` hardware backend should remain usable.
