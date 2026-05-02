# TUI Dashboard

The robot dashboard is a read-only SSH tool. It connects to `robot-telemetry.service`, shows current robot state, and tails service logs. It does not send commands or manage services.

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
- Controller: Xbox 360 connectivity, sticks, triggers, D-pad, and pressed buttons.
- Wheels: normalized left/right command, target QPPS, actual encoder QPPS, error, current, and read status.
- Logs: `journalctl -u robot-telemetry -u gamepad-teleop -u robot-brain -f -n 100`.

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
