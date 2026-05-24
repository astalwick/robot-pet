# Personality Phase 2 — Simple Tool Calling

## Status

Complete.

## Goal

Let the cloud LLM call small robot tools through the Pi conversational loop.

This phase proves the path from voice to LLM decision to local robot action. The tools are intentionally tiny and bounded.

## Entry Criteria

- Personality Phase 1 conversation loop works.
- Body Phase 0 motion is reliable.
- The Pi can execute local tool handlers from the conversation service.

## Exit Criteria

This phase is done when:

- The LLM can choose a tool call in response to user speech.
- The Pi receives and validates the tool call.
- At least one motion tool executes locally.
- Tool results are returned to the conversation loop.
- Failures are spoken back to the user.

## Shipped Scope

Two motion tools exposed to the LLM:

- `wiggle()` — short side-to-side body shake (`±0.5` angular_z, ~0.5s total).
- `move_forward()` — short forward nudge (`0.3` linear_x, ~0.5s).

Both are time-bounded, parameterless, and produce small wheel speeds via the existing `DifferentialDriveMixer` (speed_scale=0.25). The `switch_voice` tool from Phase 1 stays in place.

### Architecture

- The voice service (`robot-voice`) never touches motor hardware. It calls a tiny Unix-socket motion-intent protocol (`/run/robot-pet/motion-intent.sock`).
- A pure-state executor (`src/control/motion_intent.py`) decides per control-loop tick whether to emit a `MotionCommand`, finish, or be preempted.
- `gamepad-teleop` temporarily hosts the executor and the intent socket — it polls the bridge each tick, runs the executor through the same mixer that gamepad input uses, and reports the outcome back to the voice client. Body Phase 2 replaces this with a dedicated `robot-motion` service that owns the RoboClaw.

### Gamepad Arbitration

Gamepad always wins:

- If the gamepad stick is active when an intent request arrives, the very next executor tick preempts the intent and returns `preempted_by_gamepad`.
- If the gamepad becomes active mid-intent, the same preemption path runs.
- Per-tick command selection prefers `gamepad_command` whenever the stick is non-zero.

### Failure Speech

The system prompt instructs the assistant to briefly explain failures in friendly language (for example, "I tried, but the gamepad cut me off"). The motion-intent client never raises — it always returns a JSON dict the LLM can read.

## Cross-Track Dependencies

- Uses Body Phase 0 for reliable gamepad-proven motion.
- Does not require Body Phase 2 safety sensors because movement is proof-of-concept and tightly bounded.
- Body Phase 2 replaces `gamepad-teleop` as the motion executor with a proper `robot-motion` service.
- Real tools like `navigate_to("kitchen")` wait for Body Phase 5.

## Not In Scope

- Continuous LLM-owned motion.
- Autonomous navigation.
- Long-running actions without local supervision.
- Tool access to unsafe hardware primitives.

## Notes

The LLM requests actions. The Pi is the execution and safety boundary. Raw motor commands are not exposed as LLM tools.
