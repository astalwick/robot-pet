# robo-pet

A Raspberry Pi 5 robot: differential drive, voice interaction, eventually autonomous navigation.

## Quick Start

**From your Mac**, with a fresh Pi 5 running Raspberry Pi OS Lite:

```bash
./initialize-pi.sh
```

This sets up SSH keys, installs dependencies, configures UART, and starts the robot-brain service.

**On the Pi**, once hardware is connected:

```bash
source ~/robot-pet/.venv/bin/activate
python scripts/test-motor.py  # Verify motors work
```

## Project Structure

```
├── initialize-pi.sh       # Run from Mac to set up a fresh Pi
├── setup.sh               # Run on Pi (called by initialize-pi.sh)
├── systemd/               # Service unit files
│   ├── robot-brain.service
│   ├── robot-telemetry.service
│   └── gamepad-teleop.service
├── src/
│   ├── config/            # Persistent runtime config helpers
│   ├── control/           # Teleop policy and differential drive mixing
│   ├── drivers/           # Hardware drivers (pure Python, ROS2-ready)
│   │   ├── motor.py       # RoboClaw motor controller
│   │   └── controller.py  # Xbox 360 gamepad
│   ├── telemetry/         # Local JSON/socket telemetry helpers
│   ├── lib/               # Shared utilities
│   │   └── log.py         # Logging setup
│   ├── robot_brain.py     # Main orchestrator service
│   ├── robot_telemetry.py # Local telemetry hub
│   ├── robot_dashboard.py # SSH Textual dashboard
│   └── gamepad_teleop.py  # Boot-ready gamepad teleop service
├── scripts/               # Manual tools and hardware diagnostics
│   ├── diagnostics/
│   ├── test-motor.py
│   └── redeploy-robot.sh
└── docs/
    ├── ARCHITECTURE.md    # System design, ROS2 migration path
    └── phases/            # Long-term roadmap, phase docs, and build planning
        ├── bom-by-phase.md
        ├── index.md
        ├── phase-0-assembly-guide.md
        ├── robot-build-gotchas.md
        └── legacy/
```

## Hardware

- **Brain:** Raspberry Pi 5 (4GB)
- **Motion:** RoboClaw 2x7A + goBILDA 5203 motors + 96mm wheels
- **Power:** 3S LiPo (motors) + USB-C power bank (Pi)

See `docs/phases/index.md` for the roadmap and `docs/phases/bom-by-phase.md` for the full BOM.

## Architecture

Designed for eventual ROS2 migration. Drivers in `src/drivers/` are pure Python classes with no framework dependencies—they'll drop directly into ROS2 nodes. The current systemd services are temporary scaffolding.

See `docs/ARCHITECTURE.md` for details.

## Development

```bash
# Deploy changes to Pi
ssh pi@robot-pi.local 'cd ~/robot-pet && git pull && ./setup.sh'

# View logs
ssh pi@robot-pi.local 'journalctl -u robot-brain -f'
```
