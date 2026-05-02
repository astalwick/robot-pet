# TUI Dashboard

The robot dashboard is an SSH tool for robot status, service logs, redeploys, and drive tuning. It connects to `robot-telemetry.service`, shows current robot state, tails service logs, and can restart `gamepad-teleop.service` after saving tuning changes.

## Start The Dashboard

From the Pi:

```bash
cd ~/robot-pet
source .venv/bin/activate
PYTHONPATH=src python src/robot_dashboard.py
```

The dashboard expects `robot-telemetry.service` to be running and listening on `/run/robot-pet/telemetry-sub.sock`.

## Panels

- Pi health: uptime, 1-minute load, memory, disk, SoC temperature, throttling flags, and Pi power-bank charge status.
- Motor battery: RoboClaw main battery voltage, estimated 3S per-cell voltage, and status.
- Drive tuning: normal speed, turbo speed, turn scale, per-stick deadzones, and acceleration slew.
- Controller: Xbox 360 connectivity, sticks, triggers, D-pad, and pressed buttons.
- Wheels: normalized left/right command, target QPPS, actual encoder QPPS, error, current, and read status.
- Logs: `journalctl -u robot-telemetry -u gamepad-teleop -u robot-brain -f -n 100`.

## Drive Tuning

Drive tuning is stored at `/home/pi/.config/robot-pet/teleop.json`. The dashboard edits the desired values locally, then writes the file and restarts `gamepad-teleop.service` when you press `a`.

Keys:

- `↑` / `↓`: select a tuning row.
- `-` / `=`: decrease or increase the selected value.
- `a`: save tuning and restart `gamepad-teleop.service`.

The active values are also published by `gamepad-teleop.service` in telemetry, so the dashboard can show when displayed values differ from the running service.

## Battery Status

Motor battery status uses simple 3S LiPo voltage bands:

- `ok`: pack voltage is at or above 10.5 V.
- `low`: pack voltage is below 10.5 V.
- `critical`: pack voltage is at or below 9.6 V.
- `unknown`: RoboClaw voltage read failed.

The RoboClaw cutoff is configured around 9.6 V, so the dashboard warns before the cutoff point.

## Pi Power Bank

The Pi is powered by a USB-C PD power bank, but the current build has no readable fuel gauge or UPS HAT. The dashboard intentionally shows power-bank charge as unavailable instead of inventing a percentage.

Useful Pi-side signals are still shown: undervoltage/throttling flags, SoC temperature, load, memory, disk, and uptime.

## Service Logs

Inspect the telemetry hub directly with:

```bash
sudo systemctl status robot-telemetry
journalctl -u robot-telemetry -f
```

Inspect all dashboard-related services with:

```bash
journalctl -u robot-telemetry -u gamepad-teleop -u robot-brain -f
```
