# Body Phase 0 — Reliable Manual Platform

## Status

Complete.

## Goal

Build the minimum physical robot that can move reliably under direct human control.

This phase is the foundation for every later body and personality phase. The robot does not need autonomy, camera, microphone, speech, mapping, or personality yet. It needs to be a trustworthy moving base.

## What Exists

- Raspberry Pi 5 as the onboard computer.
- goBILDA differential-drive chassis.
- Two goBILDA 5203 Yellow Jacket motors with encoders.
- RoboClaw 2x7A motor controller.
- 96mm wheels and rear ball caster.
- Separate Pi and motor power rails.
- Xbox gamepad teleop as the supported manual driving path.
- Local telemetry and dashboard scaffolding for observing the running robot.

## Exit Criteria

This phase is done when:

- The robot drives reliably via gamepad.
- The deadman behavior is understood and trusted.
- Forward, reverse, and turning directions match the gamepad controls.
- The RoboClaw and motor signs are proven with the real hardware.
- The base is stable enough to carry the next round of hardware.

## Key Decisions

- Use a RoboClaw instead of a dumb H-bridge so encoder reading and velocity PID live close to the motors.
- Keep hardware drivers framework-agnostic so they can survive a future ROS2 migration.
- Use systemd services as temporary scaffolding until ROS2 becomes worth the overhead.
- Keep Pi and motor power on separate rails for now, with common ground.
- Treat gamepad motion as the known-good manual control path.

## Not In Scope

- Voice interaction.
- Autonomous movement.
- Obstacle avoidance.
- SLAM or navigation.
- Docking.
- Personality beyond whatever the operator projects onto a moving robot.

## Related Docs

- [Phase 0 assembly guide](phase-0-assembly-guide.md)
- [BOM by phase](bom-by-phase.md)
- [Legacy shopping list and hardware decisions](legacy/robot-shopping-list.md)
- [Build gotchas and risks](robot-build-gotchas.md)
