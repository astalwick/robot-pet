# Gamepad Teleop

Boot-ready gamepad teleop runs as `gamepad-teleop.service`. It waits for the Xbox 360 controller and `robot-motion`, then sends drive commands while RB is held. `robot-motion.service` owns the RoboClaw and applies safety gating from range sensors.

## Safety Checklist

- Put the wheels up for first tests after any wiring, RoboClaw config, or sign-convention change.
- Start with the default `--speed-scale 0.25`.
- Keep RB released until you are ready for motion.
- Confirm the RoboClaw is powered and `ReadVersion` works.
- Release RB, unplug the controller, or stop the service to command zero speed.

## Diagnostics

Diagnostics are hardware bring-up scripts, not the normal driving path. Run them from the repo root with the venv active:

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

The service publishes dashboard telemetry to `robot-telemetry.service` at 5 Hz, including the active drive tuning. Telemetry is best-effort; if the hub is stopped or restarting, driving continues.

Restart it after changing code:

```bash
sudo systemctl restart gamepad-teleop
```

Voice motion tools (`wiggle`, `move_forward`) use `/run/robot-pet/motion-intent.sock` on `robot-motion.service`. If voice reports `motion_socket_missing`, check `journalctl -u robot-motion` for `motion intent socket at` or `motion intent bridge unavailable`. Restarting `robot-motion` recreates the socket.

Gamepad wheel commands go to `/run/robot-pet/motion-drive.sock`. If teleop logs `waiting for robot-motion`, start or restart `robot-motion.service` first.

## Foreground Debugging

Stop the services before running a foreground teleop process so only one process owns the RoboClaw:

```bash
sudo systemctl stop gamepad-teleop robot-motion
cd ~/robot-pet
source .venv/bin/activate
PYTHONPATH=src python src/gamepad_teleop.py --speed-scale 0.15
```

Useful flags:

- `--config /home/pi/.config/robot-pet/drive_tuning.json`
- `--device /dev/input/eventX`
- `--port /dev/serial0`
- `--address 0x80`
- `--baud 38400`
- `--qpps 2425`
- `--speed-scale 0.25`
- `--turbo-scale 0.75`
- `--turn-scale 1.0`
- `--left-stick-deadzone 0.15`
- `--right-stick-deadzone 0.15`

Acceleration shaping (slew) is owned by `robot-motion`, which reads the `qpps_slew_limit` from the shared drive tuning config; the gamepad sends raw targets and has no slew flag.

Drive tuning defaults are loaded from `/home/pi/.config/robot-pet/drive_tuning.json` when present. The dashboard writes this file and restarts `gamepad-teleop.service` and `robot-motion.service` to apply changes.

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
