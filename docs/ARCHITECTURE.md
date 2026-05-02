# Architecture

## Overview

This project runs on a Raspberry Pi as a robot controller. The current implementation uses systemd services for process management, but the architecture is designed for future migration to ROS2.

## Guiding Principle

**Drivers are framework-agnostic. Services are throwaway scaffolding.**

The Python code that talks to hardware (motors, sensors) should be pure Python classes with no dependencies on systemd, ROS2, or any orchestration framework. The systemd services we build now are temporary wrappers that will be replaced by ROS2 nodes later.

## Directory Structure

```
src/
├── control/              # Framework-agnostic teleop policy and drive mixing
│   ├── commands.py       # MotionCommand, WheelCommand, WheelSpeedCommand
│   ├── differential_drive.py
│   └── teleop.py
├── drivers/              # Hardware drivers - PURE PYTHON, survives ROS2 migration
│   ├── __init__.py
│   ├── motor.py          # MotorDriver class
│   └── controller.py     # ControllerDriver class
├── lib/                  # Shared utilities
│   └── log.py            # Logging setup (journald-friendly)
├── robot-brain.py        # systemd service - orchestrator (temporary)
└── gamepad_teleop.py     # systemd service - boot-ready gamepad teleop (temporary)

systemd/
└── *.service             # Unit files for each service
```

## Driver Guidelines

Drivers in `src/drivers/` must:

1. **Be pure Python classes** - no framework imports (no ROS, no systemd awareness)
2. **Have no logging framework dependency** - accept an optional logger, or don't log at all
3. **Be testable in isolation** - can instantiate and test without running any service
4. **Expose clean interfaces** - methods like `set_speed(left, right)`, not `handle_ros_message(msg)`

Example driver:

```python
class MotorDriver:
    def __init__(self, left_pin: int, right_pin: int):
        # GPIO setup
        ...
    
    def set_speed(self, left: float, right: float):
        """Set wheel speeds. Range: -1.0 (full reverse) to 1.0 (full forward)."""
        ...
    
    def stop(self):
        """Immediately stop both motors."""
        ...
    
    def cleanup(self):
        """Release GPIO resources."""
        ...
```

## Service Guidelines

Systemd services in `src/robot-*.py` are thin wrappers:

1. **Import and instantiate drivers**
2. **Handle service lifecycle** (startup, shutdown, crash logging)
3. **Implement whatever IPC is needed** (for now - this gets replaced by ROS2 topics later)

Keep services minimal. Business logic belongs in drivers or separate modules, not in the service wrapper.

## ROS2 Migration Path

When migrating to ROS2:

1. `src/drivers/` → unchanged, just import into ROS2 nodes
2. `src/robot-*.py` → deleted, replaced by ROS2 nodes
3. `systemd/` → deleted, replaced by ROS2 launch files
4. `src/lib/log.py` → deleted, use ROS2 logging

The ROS2 structure will look like:

```
src/
├── drivers/              # unchanged!
│   ├── motor.py
│   └── sensor.py
├── ros_nodes/
│   ├── motion_node.py    # subscribes to /cmd_vel, calls MotorDriver
│   └── sensors_node.py   # publishes to /scan, uses SensorDriver
└── launch/
    └── robot.launch.py   # starts all nodes
```

## Current Services

| Service | Purpose | Status |
|---------|---------|--------|
| `robot-brain` | Orchestrator / behavior state machine | Stub |
| `gamepad-teleop` | Xbox controller to RoboClaw closed-loop speed teleop | Active |
| `robot-motion` | Autonomous motor control | Not yet created |
| `robot-sensors` | Sensor reading | Not yet created |

## Logging

All services log to stdout/stderr. systemd captures output to journald.

- View logs: `journalctl -u robot-brain -f`
- View gamepad teleop logs: `journalctl -u gamepad-teleop -f`
- View multiple: `journalctl -u robot-brain -u gamepad-teleop -f`
- Format: `LEVEL service-name: message`

Timestamps come from journald, not the application.
