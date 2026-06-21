# robo-pet

A Raspberry Pi 5 robot: differential drive, live camera, gamepad teleop, browser dashboard, SSH operator dashboard and voice interaction.

## Quick Start

**From your Mac**, with a fresh Pi 5 running Raspberry Pi OS Lite:

```bash
./initialize-pi.sh
```

This sets up SSH keys, installs dependencies, configures UART, and installs the systemd services.

**On the Pi**, once hardware is connected:

```bash
source ~/robot-pet/.venv/bin/activate
python scripts/test-motor.py  # Verify motors work
```

## Operator Surfaces

Two operator UIs ship today; both read from `robot-telemetry.service` and can write drive tuning + restart `gamepad-teleop.service` and `robot-motion.service` (both read the tuning config at startup).

- **Web dashboard** — `http://<pi-host>:8080/`. Live MJPEG camera, telemetry, service logs, redeploy, and drive tuning. Default surface from a laptop.
- **SSH TUI** — `python src/robot_dashboard.py` on the Pi. Same telemetry/logs/redeploy/tuning, no video. Emergency surface when the network or browser is unavailable.

See `docs/tui-dashboard.md` and `docs/gamepad-teleop.md` for details.

## Project Structure

```
├── initialize-pi.sh       # Run from Mac to set up a fresh Pi
├── setup.sh               # Run on Pi (called by initialize-pi.sh)
├── systemd/               # Service unit files
│   ├── robot-brain.service
│   ├── robot-telemetry.service
│   ├── robot-camera.service
│   ├── robot-vision.service
│   ├── robot-web-dashboard.service
│   └── gamepad-teleop.service
├── src/
│   ├── config/            # Persistent runtime config helpers (drive tuning)
│   ├── control/           # Teleop policy and differential drive mixing
│   ├── drivers/           # Hardware drivers (pure Python, ROS2-ready)
│   │   ├── motor.py       # RoboClaw 2x7A motor controller
│   │   ├── controller.py  # Xbox 360 gamepad
│   │   └── camera.py      # Pi camera (picamera2)
│   ├── telemetry/         # Local JSON/socket telemetry helpers
│   ├── lib/log.py         # Logging setup (journald-friendly)
│   ├── robot_brain.py         # Orchestrator service (stub)
│   ├── robot_telemetry.py     # Local telemetry hub
│   ├── robot_camera.py        # Pi camera owner; serves MJPEG/snapshot on :8081
│   ├── robot_vision.py        # Polls camera snapshots, runs face detection
│   ├── robot_web_dashboard.py # Browser dashboard on :8080
│   ├── robot_dashboard.py     # SSH Textual dashboard
│   ├── gamepad_teleop.py      # Boot-ready gamepad teleop service
│   └── web_dashboard_static/  # HTML/CSS/JS for the browser dashboard (no build step)
├── scripts/               # Manual tools and hardware diagnostics
│   ├── diagnostics/       # Controller and RoboClaw bring-up scripts
│   ├── test-motor.py
│   └── redeploy-robot.sh
└── docs/
    ├── ARCHITECTURE.md    # System design, ROS2 migration path
    ├── gamepad-teleop.md  # Teleop service, controls, tuning, foreground debug
    ├── tui-dashboard.md   # SSH TUI usage
    ├── phases/            # Long-term roadmap and BOM by phase
    ├── plans/             # Per-feature implementation plans
    └── ideas/             # Scratch / pre-plan notes
```

## Hardware

- **Brain:** Raspberry Pi 5 (4GB)
- **Motion:** RoboClaw 2x7A + goBILDA 5203 motors + 96mm wheels
- **Vision:** Raspberry Pi Camera (picamera2)
- **Input:** Xbox 360 wireless controller
- **Power:** 3S LiPo (motors) + USB-C power bank (Pi)

See `docs/phases/index.md` for the roadmap and `docs/phases/bom-by-phase.md` for the full BOM.

## Architecture

Designed for eventual ROS2 migration. Drivers in `src/drivers/` are pure Python classes with no framework dependencies — they'll drop directly into ROS2 nodes. The current systemd services are temporary scaffolding that map naturally onto future ROS2 nodes (camera node, motion node, etc.).

See `docs/ARCHITECTURE.md` for details.

## Development

**Local unit tests** (Mac or Linux, no robot hardware):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m unittest discover tests
```

`evdev` is Linux-only (gamepad on the Pi); it is omitted on macOS but the unit suite still runs fully.

```bash
# Redeploy on the Pi (pulls, reinstalls, restarts services)
ssh pi@robot-pi.local '~/robot-pet/scripts/redeploy-robot.sh'

# Or trigger redeploy from either operator dashboard (web at :8080, or the SSH TUI).

# View logs for everything operator-visible
ssh pi@robot-pi.local 'journalctl -u robot-brain -u robot-telemetry -u gamepad-teleop -u robot-camera -u robot-vision -u robot-web-dashboard -f'
```
