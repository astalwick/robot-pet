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
- Front bump switches can detect physical contact missed by ToF.
- Cliff sensors can detect floor drop-offs.
- IMU orientation is readable and stable enough to use in motion logic.
- A single `robot-motion` service owns the RoboClaw and is the only process that drives motors. All motion sources (gamepad, voice tools, future autonomy) publish intents to it.
- Local software can command a stop or refuse movement based on safety sensor state, enforced inside `robot-motion`.
- The Pi can cut power to the motor battery rail through a high-side MOSFET switch when the robot is sleeping or the RoboClaw reports low LiPo voltage.
- A `robot-battery` process owns the MOSFET GPIO and applies the motor-rail power policy (see below).
- Gamepad control remains usable while safety state is observable.

## Default Direction

- Use VL53L1X ToF sensors for forward obstacle sensing.
- Add simple front bump switches wired directly to Pi GPIO as a contact fallback to ToF.
- Use downward-facing ToF sensors for cliff detection.
- Use a BNO085-class IMU with onboard fusion rather than writing sensor fusion from raw gyro/accelerometer data.
- Add a Pi-controlled high-side MOSFET switch in the existing motor battery positive rail, after the fuse and manual switch and before the RoboClaw.
- Stand up `robot-battery` as the sole owner of the MOSFET enable line and motor-rail power policy. It stays alive when the LiPo rail is off; `robot-motion` cannot do that job because it needs a powered RoboClaw for serial.
- Use RoboClaw main-battery voltage and motor-current telemetry (published by `robot-motion` while the rail is up) for policy decisions.
- Keep safety logic local to the Pi.
- Stand up `robot-motion` as the single owner of `MotorDriver` / RoboClaw. `gamepad-teleop` is reworked to publish intents to it instead of driving motors directly. Safety gating lives inside `robot-motion` so every motion source is protected by the same rules.

### Forward ToF prototype mount

Initial VL53L1X obstacle sensor layout to test:

- Mount one center sensor facing straight forward.
- Mount left and right sensors about **100 mm** from center, angled about **15 degrees** outward.
- Put the sensor faces roughly parallel with or just ahead of the existing camera mast, about **40 mm behind the bumper front** if the bumper, wheels, and chassis stay out of the sensor field of view.
- Start around **80-100 mm** above the ground.

Treat the VL53L1X full field of view as roughly **+/- 13.5 degrees** horizontally and vertically. The mount should be pulled back only as far as the robot body stays out of that view; if the bumper or wheels show up in readings, move the sensor forward, outward, or higher.

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

## `robot-battery` — motor rail power

**Goal:** the robot should *feel* always on (gamepad connect → drive within ~2s) but should *aggressively* disconnect the LiPo from the RoboClaw when nothing needs the motor rail.

### Why a separate process

`robot-motion` owns RoboClaw serial and only works when the MOSFET is on. Something must decide when to energize the rail *before* motion can connect. That logic belongs in a small always-on service (or equivalent long-lived task) that owns **only** the MOSFET GPIO and policy — not motor commands, not safety gating on wheel speeds.

### Hardware boundary

- **Owns:** Pi GPIO → MOSFET enable (high-side switch on LiPo+ after fuse and manual switch).
- **Does not own:** RoboClaw UART, `MotorDriver`, or drive intents.
- **Reads:** `robot-telemetry` snapshots (gamepad connected, pack voltage, motor currents, voice/LLM session state).
- **Writes:** MOSFET on/off; optional “stop motors” request to `robot-motion` before cutting power (same pattern as idle shutdown: stop first, brief settle, then rail off).

When the MOSFET is off, the RoboClaw is unpowered — there is no live pack voltage over serial. **Do not** gate energize on a stale “last known” voltage; after a recharge the only way to know pack state is to energize briefly and read RoboClaw telemetry again.

### Power-on / power-off sequence

**Energize:** assert MOSFET ON → `robot-motion` polls RoboClaw quickly (e.g. 50–100ms, not the normal 1s retry) until `ReadVersion` / zero-speed succeeds → read fresh pack voltage from RoboClaw (via motion telemetry). If pack &lt; **11.0 V** (draft threshold): surface a **low battery** warning on the dashboard, stop, settle, MOSFET OFF — do not leave the rail up to drive. Above RoboClaw’s configured **9.6 V** hardware cutoff; 11.0 V is software headroom, not a substitute for the RoboClaw cutoff.

**De-energize:** command stop (or wait for zero speed) → short settle (~250ms) → MOSFET OFF.

Target seam from “I want to drive” to wheels moving: **≤ ~2s** when voltage is OK (acceptable; not invisible). A low pack may add a short “power up → read → shut down” cycle before drive is allowed.

### Policy brainstorm (not decided)

**The table below is early brainstorming only.** Numbers, delays, and which signals count as “idle” or “voice active” are **not** agreed policy. Expect revision before implementation; do not treat any row as a spec or exit criterion by itself.

Directional intent only: the robot should *feel* ready when you pick up the gamepad, but should *aggressively* drop the LiPo rail when nothing needs it. Wake reasons are probably OR; sleep probably needs debouncing.

| Idea (draft) | Sketch |
| ------------ | ------ |
| Pack &lt; **11.0 V** after energize + RoboClaw read | Dashboard warning; stop → settle → rail off (not “refuse forever” from stale telemetry). |
| Gamepad **disconnected → connected** | Energize rail; fast RoboClaw wake. |
| Gamepad **connected → disconnected** | Maybe wait **~2 s**, then de-energize? |
| Gamepad connected but **idle a long time** | Maybe de-energize after stop + settle? |
| Voice / LLM recently active | Maybe keep rail up for motion / “listening”? |
| High motor current | Maybe emergency rail off — thresholds TBD vs **20 A** fuse. |
| Pack low while rail is up | Maybe align with dashboard **low** / RoboClaw cutoff — TBD. |

When this firms up, thresholds belong in config (e.g. `battery.json`), not scattered magic numbers.

### Interaction with other services

```text
gamepad-teleop ──drive intents──► robot-motion ──UART──► RoboClaw (when rail up)
       │                                │
       └── telemetry ──► robot-telemetry ◄── telemetry ──┘
                                ▲
robot-battery ──GPIO MOSFET─────┘ (subscribe only; publish rail state)
robot-voice ── session state ───► telemetry
```

`gamepad-teleop` does **not** toggle the MOSFET. It keeps reading the controller whenever USB is up; `robot-battery` maps “connected / idle / voice active” to rail state.

### Exit criteria (additions)

- `robot-battery` exists, owns MOSFET GPIO, publishes `motor_rail` state on telemetry (exact states TBD).
- With manual switch on: energizing the rail and cutting it again works; fresh RoboClaw pack voltage is visible after energize; sub-11.0 V (or whatever threshold we pick) yields a dashboard warning and rail off without requiring a stale “last known” voltage.
- Specific wake/sleep/overcurrent rules are **not** locked until the brainstorm table is revised and signed off.

## Notes

The goal is a safety layer, not clever behavior. Prefer simple, inspectable rules: stop, refuse, limit motion, or cut the motor rail when local sensors say the body or battery is at risk.

The manual motor power switch remains the trusted hard cutoff. The Pi-controlled MOSFET is for routine sleep and forgotten-switch protection: stop motion first, briefly wait for the RoboClaw to settle, then disconnect motor battery power. `robot-battery` is the intended owner once policy is decided.
