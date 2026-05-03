# Personality Phase 4 — Emotional State

## Goal

Add persistent emotional or mood state that influences the robot's speech and behavior over time.

This turns personality from a static prompt into a small internal state machine.

## Entry Criteria

- Personality Phase 1 conversation loop works.
- Personality Phase 3 character direction is clear enough to define moods that fit the robot.
- Tool calling from Personality Phase 2 is useful if emotions should affect body behavior.

## Exit Criteria

This phase is done when:

- The robot has explicit emotional state such as curious, sleepy, excited, bored, cautious, or annoyed.
- State changes are caused by observable events, conversations, or time.
- Mood influences LLM context.
- Mood influences at least one visible behavior, speech pattern, or tool choice.
- The state survives at least across a running session.

## Default Direction

- Keep emotional state deterministic and inspectable.
- Let the LLM explain and express the state, but do not make the LLM the only source of truth for it.
- Start with a small number of states before adding nuance.

## Cross-Track Dependencies

- Benefits from Body Phase 0 because moods can produce tiny gestures.
- Benefits from Body Phase 1 because voice gives emotion a natural output.
- Later, Body Phase 5 and Personality Phase 6 can make mood influence self-initiated movement.

## Not In Scope

- Deep long-term memory.
- Complex psychology.
- Unbounded autonomous behavior.
- Display expressions unless Personality Phase 5 is also underway.

## Notes

The emotion system should be a tinkering surface. It should be easy to add a new mood, trigger, or expression without rewriting the conversation stack.
