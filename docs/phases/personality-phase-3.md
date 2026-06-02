# Personality Phase 3 — Visible Personality

## Status

Complete.

## Goal

Give the robot a character card or personality system prompt that visibly changes how it speaks and behaves.

The goal is not just "the prompt exists." The goal is that a human can tell the robot has a consistent style.

## Entry Criteria

- Personality Phase 1 conversation loop works.
- Tool calling from Personality Phase 2 is useful but not strictly required.

## Exit Criteria

This phase is done when:

- The robot has a documented personality card.
- The card affects speech patterns, preferences, and reactions.
- The effect is obvious in repeated conversations.
- The robot can maintain the same character across a session.
- The personality can influence available expression tools if Personality Phase 2 exists.

## Default Direction

- Start with a system prompt or character card.
- Keep it inspectable and easy to edit.
- Bias toward pet-like presence rather than assistant-like obedience.
- Include preferences, quirks, speech style, and boundaries.

## Cross-Track Dependencies

- Needs Personality Phase 1.
- Benefits from Personality Phase 2 because tool calls make personality visible in the body.
- Does not require autonomy, mapping, or safety sensors.

## Not In Scope

- Persistent emotional state.
- Long-term memory.
- Autonomous self-initiated behavior.
- Display expressions unless the hardware already exists.

## Notes

This phase can happen before, after, or alongside simple tool calling. If conversation starts feeling generic, pull this phase forward.
