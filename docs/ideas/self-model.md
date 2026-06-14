> DO NOT CONSIDER THESE TO BE CONCRETE PLANS. ANY OPINIONS HERE ARE HALLUCINATED BY AI.

# Self-Model / Introspection Plan

## Goal

Give Bloop an honest sense of its own body and recent actions, so it can say true
things about itself: "I'm low on battery," "I just bumped something," "I can't move
right now." This is the robot's view of itself, drawn from telemetry it already
publishes, made available to the LLM.

This is the "third state layer" [[personality]] deferred. It also feeds
[[proactive-behavior]] (self-state changes are triggers) and [[embodied-affect]]
(state selects a gesture), and overlaps [[memory]] (some self-events are worth
remembering).

## What exists today

The robot publishes telemetry — battery, motion state, sensor / collision signals,
what the last command did — through the telemetry hub. None of it currently reaches
the LLM's prompt; the model has no grounded view of its own body.

## Mechanics

- **Gather.** Collect current self-state from existing telemetry: battery level,
  moving / idle / stuck, last action and whether it succeeded, recent collision or
  preemption.
- **Summarize into a state block.** Render a compact, current self-description and
  inject it as the state layer of the composed prompt ([[personality]]'s
  `<character>` / `<operational>` / `<state>` ordering).
- **Emit events.** Surface meaningful changes (battery crossed low, got stuck, got
  unstuck) as events other layers can react to.

## Brainstorm — directions

- **Snapshot vs. events.** Maintain a current snapshot for the prompt *and* emit
  change events for [[proactive-behavior]] / [[embodied-affect]].
- **Truthful grounding.** The state block carries facts; characters in
  [[personality]] may color the delivery but not contradict the fact ("battery
  low" can't become "I feel great").
- **Recent-action trace.** A short rolling log of the last few actions and outcomes
  the model can reference ("I already tried forward and hit something").
- **Capability awareness.** Expose what the robot currently *can't* do (motion
  unavailable, camera down) so it doesn't promise actions it can't perform.
- **Salient self-memory.** Notable self-events (kept getting stuck here) can be
  written to [[memory]] rather than living only in the moment.
- **Compactness.** The block has to stay small — current state, not a telemetry
  dump — to avoid drowning the character.

## Open questions

1. **What goes in the block?** Which telemetry is worth the prompt budget, and at
   what granularity (exact battery % vs. "low")?
2. **Truth vs. character.** How firmly is the underlying fact protected from
   characterful rewording?
3. **Snapshot freshness.** Rebuilt every turn, or pushed on change?
4. **Action trace length.** How many past actions, and when do they age out?
5. **What becomes memory?** Which self-events graduate from transient state into
   durable [[memory]]?
6. **Ownership.** Does the voice service assemble the block from telemetry, or does
   a small introspection component publish a ready-made self-summary?

## Relationship to existing code

Self-model reads telemetry the robot already publishes and turns it into a prompt
block and a set of events. It is the concrete form of [[personality]]'s deferred
state layer, a trigger source for [[proactive-behavior]], an input to
[[embodied-affect]], and an occasional writer to [[memory]]. It produces; the
other layers consume.
