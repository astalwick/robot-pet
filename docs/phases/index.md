# Robot Pet Phases

This folder tracks the long-term direction for the robot pet. The project is not one straight line. It has two related tracks that can advance independently:

- **Body / autonomy track:** the physical robot platform, local control, sensing, perception, SLAM, navigation, docking, and future hardware expansions.
- **Personality / agency track:** voice interaction, cloud LLM conversation, tool calling, character, emotional state, expressions, and eventually self-initiated behavior.

The north star is a home robot pet: something that feels like a small being in the house, while staying modular enough to become a general tinkering platform for future experiments.

## Architecture Direction

Default direction:

- Raspberry Pi owns local robot control, low-latency body behavior, hardware access, and safety-critical execution.
- A networked Apple MacBook M3 Pro can offload heavier work such as vision, perception, and possibly SLAM.
- Cloud services provide LLM reasoning, conversation, tool calling, STT/TTS where useful, and personality.
- The Pi exposes bounded robot tools to the LLM. The LLM requests actions; the Pi validates, executes, monitors, and can refuse or stop.
- ROS2 / Nav2 are the expected path once SLAM and navigation become the main work. Until then, keep pure Python drivers and thin service wrappers.

These are opinionated defaults, not permanent commitments. Change them when reality gives a better answer.

## Current Status

| Track | Phase | Status | Meaning |
|---|---:|---|---|
| Body / autonomy | [Phase 0](body-phase-0.md) | Complete | Reliable gamepad-driven base robot |
| Body / autonomy | [Phase 1](body-phase-1.md) | Complete | Audio/video hardware installed on the moving platform |
| Body / autonomy | [Phase 2](body-phase-2.md) | Next / near-term | Local safety sensing |
| Personality / agency | [Phase 1](personality-phase-1.md) | In progress | Hear and speak via STT -> cloud LLM -> TTS; wake word still open |

## Body / Autonomy Track

| Phase | Name | Core Deliverable |
|---:|---|---|
| [0](body-phase-0.md) | Reliable Manual Platform | Robot moves reliably via gamepad |
| [1](body-phase-1.md) | Audio / Video Body | Mic, speaker, and camera are installed and usable |
| [2](body-phase-2.md) | Local Safety Sensing | Obstacle, cliff, and orientation sensing protect motion |
| [3](body-phase-3.md) | Perception Offload | Camera frames reach the MacBook and detections return |
| [4](body-phase-4.md) | SLAM and Localization | Robot can build and localize against a map |
| [5](body-phase-5.md) | Autonomous Navigation | Robot can navigate to map goals |
| [6](body-phase-6.md) | Docking and Self-Recharge | Robot can find and use a charging dock |
| [7+](body-phase-7-plus.md) | Expansion Platform | Arms, richer embodiment, and experimental hardware |

Hardware is bought on demand. Do not install every sensor up front; each body phase adds the hardware needed for that capability.

## Personality / Agency Track

| Phase | Name | Core Deliverable |
|---:|---|---|
| [1](personality-phase-1.md) | Hear and Speak | Voice conversation works through STT -> cloud LLM -> TTS |
| [2](personality-phase-2.md) | Simple Tool Calling | LLM can call small bounded robot tools |
| [3](personality-phase-3.md) | Visible Personality | A character card visibly changes speech and behavior |
| [4](personality-phase-4.md) | Emotional State | Persistent mood/emotion influences responses and actions |
| [5](personality-phase-5.md) | Expression Display | Onboard display reflects personality and emotion |
| [6](personality-phase-6.md) | Agency Loop | Robot can initiate actions and monitor its own state |

These phases are more swappable than the body phases. For example, the personality card may be useful immediately after Phase 1, before or alongside tool calling.

## Cross-Track Rule

Each phase should say what it needs from the other track. Examples:

- Personality Phase 2 can use Body Phase 0 for tiny bounded movement tools like `wiggle()` or `move_forward_small()`.
- Personality should not expose real autonomous movement tools until Body Phase 5 exists.
- Body Phase 5 can work without rich personality, but Personality Phase 6 becomes much more interesting once Body Phase 5 exists.
- Named-place memory, such as "this room is the kitchen," comes after SLAM and localization. The map comes first; semantic labels are annotations layered on top.

## Tool and Motion Guidelines

These are planning guidelines, not a production safety certification:

- The LLM may request robot actions through tools, but local code on the Pi executes them.
- Do not expose raw motor or continuous velocity control as LLM tools.
- Early motion tools should be short, bounded, and easy to interrupt.
- Manual stop or gamepad takeover should remain the most trusted control path.
- Robot services should fail toward stopped motion when practical.
- Higher-level tools such as `navigate_to()` should wrap deterministic body capabilities, not invent them in the personality layer.

## Existing Planning Docs

- [BOM by phase](bom-by-phase.md)
- [Phase 0 assembly guide](phase-0-assembly-guide.md)
- [Build gotchas and risks](robot-build-gotchas.md)
- [Legacy shopping list and hardware decisions](legacy/robot-shopping-list.md)
