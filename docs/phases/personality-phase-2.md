# Personality Phase 2 — Simple Tool Calling

## Goal

Let the cloud LLM call small robot tools through the Pi conversational loop.

This phase proves the path from voice to LLM decision to local robot action. The tools should be intentionally tiny and bounded.

## Entry Criteria

- Personality Phase 1 conversation loop works.
- Body Phase 0 motion is reliable.
- The Pi can execute local tool handlers from the conversation service.

## Exit Criteria

This phase is done when:

- The LLM can choose a tool call in response to user speech.
- The Pi receives and validates the tool call.
- At least one expression or motion tool executes locally.
- Tool results are returned to the conversation loop.
- Failures are spoken or logged clearly.

## Default Direction

Start with low-risk tools:

- `say(text)`
- `wiggle()`
- `turn_small(direction)`
- `move_forward_small()`
- `stop()`

The motion tools should move only a few centimeters or for a very short bounded duration.

Tool handlers live locally on the Pi. The LLM chooses from the exposed tools; it does not receive raw motor control.

## Cross-Track Dependencies

- Uses Body Phase 0 for reliable gamepad-proven motion.
- Does not require Body Phase 2 safety sensors because movement is proof-of-concept and tightly bounded.
- Real tools like `navigate_to("kitchen")` wait for Body Phase 5.

## Not In Scope

- Continuous LLM-owned motion.
- Autonomous navigation.
- Long-running actions without local supervision.
- Tool access to unsafe hardware primitives.

## Notes

The LLM requests actions. The Pi is the execution and safety boundary. Do not expose raw motor commands as LLM tools.
