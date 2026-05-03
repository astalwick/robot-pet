# Personality Phase 6 — Agency Loop

## Goal

Give the robot a control loop that lets it initiate actions, monitor its own state, and decide when to speak or act.

This is where the robot starts feeling less like a voice interface and more like a being sharing a space.

## Entry Criteria

- Personality Phase 1 conversation loop works.
- Personality Phase 2 tool calling exists.
- Personality Phase 3 or Phase 4 provides character or emotional state.
- The body exposes enough safe, bounded capabilities for self-initiated behavior.

## Exit Criteria

This phase is done when:

- The robot has a loop that runs without direct user prompting.
- It can inspect relevant internal state such as battery, mood, recent interactions, tool availability, and safety status.
- It can choose small actions based on that state.
- It can decide not to act.
- It can explain what it is doing when asked.
- It remains interruptible by the user.

## Default Direction

- Keep the agency loop small and observable.
- Use explicit action budgets and cooldowns.
- Prefer high-level tools over low-level primitives.
- Let deterministic code own scheduling, safety checks, and action execution.
- Let the LLM contribute personality, planning, and language.

## Cross-Track Dependencies

- Can start with Body Phase 0 and Personality Phase 2 for tiny actions.
- Becomes much more powerful after Body Phase 5 autonomous navigation.
- Should use Body Phase 2 safety status before initiating movement beyond tiny gestures.
- Can use Body Phase 6 docking as a self-maintenance behavior once it exists.

## Not In Scope

- Unsupervised whole-home autonomy before navigation and safety are reliable.
- LLM-owned continuous motor control.
- Actions that cannot be interrupted or explained.

## Notes

This phase should make the robot feel alive, not reckless. The interesting behavior is choosing when to do small meaningful things, not maximizing autonomy for its own sake.
