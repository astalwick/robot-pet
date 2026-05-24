# Body Phase 2 — Local Safety Sensing

## Goal

Give the robot local awareness of immediate physical hazards so future autonomous movement can be bounded by onboard safety.

This is still not full autonomy. It is the phase where the robot learns not to drive into obvious things, off edges, or into an avoidable low-battery discharge state.

## Entry Criteria

- Body Phase 0 is complete.
- The chassis has enough physical room and power budget for the safety sensors.
- Any audio/video hardware from Body Phase 1 is mounted well enough not to interfere with sensor placement.

## Exit Criteria

This phase is done when:

- Forward obstacle sensors can detect objects in the robot's path.
- Cliff sensors can detect floor drop-offs.
- IMU orientation is readable and stable enough to use in motion logic.
- A single `robot-motion` service owns the RoboClaw and is the only process that drives motors. All motion sources (gamepad, voice tools, future autonomy) publish intents to it.
- Local software can command a stop or refuse movement based on safety sensor state, enforced inside `robot-motion`.
- The Pi can cut power to the motor battery rail through a high-side MOSFET switch when the robot is sleeping or the RoboClaw reports low LiPo voltage.
- Gamepad control remains usable while safety state is observable.

## Default Direction

- Use VL53L1X ToF sensors for forward obstacle sensing.
- Use downward-facing ToF sensors for cliff detection.
- Use a BNO085-class IMU with onboard fusion rather than writing sensor fusion from raw gyro/accelerometer data.
- Add a Pi-controlled high-side MOSFET switch in the existing motor battery positive rail, after the fuse and manual switch and before the RoboClaw.
- Use RoboClaw main-battery voltage telemetry as the source for low-voltage motor-rail cutoff decisions while the motor rail is awake.
- Keep safety logic local to the Pi.
- Stand up `robot-motion` as the single owner of `MotorDriver` / RoboClaw. `gamepad-teleop` is reworked to publish intents to it instead of driving motors directly. Safety gating lives inside `robot-motion` so every motion source is protected by the same rules. This is the natural birthplace for the chokepoint; ARCHITECTURE.md lists `robot-motion` as "Not yet created" and this is the phase that creates it.

## Cross-Track Dependencies

- Personality Phase 2 does not require this phase for tiny proof-of-concept tools. It will ship with `gamepad-teleop` temporarily acting as the executor for voice motion intents; this phase replaces that arrangement with a proper `robot-motion` service.
- Richer movement tools should wait for this phase or later.
- Personality Phase 6 needs safety status if it can initiate actions on its own.

## Not In Scope

- Lidar.
- SLAM.
- Navigation to goals.
- Semantic understanding of rooms or objects.
- Letting an LLM own continuous motion control.

## Notes

The goal is a safety layer, not clever behavior. Prefer simple, inspectable rules: stop, refuse, limit motion, or cut the motor rail when local sensors say the body or battery is at risk.

The manual motor power switch remains the trusted hard cutoff. The Pi-controlled MOSFET switch is for routine sleep and forgotten-switch protection: stop motion first, briefly wait for the RoboClaw to settle, then disconnect motor battery power.
