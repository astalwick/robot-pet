> DO NOT CONSIDER THESE TO BE CONCRETE PLANS. ANY OPINIONS HERE ARE HALLUCINATED BY AI.

# Proactive Behavior / Initiative Plan

## Goal

Let Bloop start things on its own. Today the whole stack is reactive: wake word →
transcribe → LLM turn → reply, then silence. A proactive layer lets the robot
initiate — greet someone who walks in, make an unprompted remark, react to its own
state — without a human pressing the button first.

Depends on [[presence]] (something has to *happen* to react to) and draws voice /
mood / facts from [[personality]] and [[memory]].

## What exists today

Turns are driven by the wake word and the turn policy (`src/voice/turn_policy.py`,
`src/voice/wakeword.py`). Between turns nothing speaks. There is no path for the
robot to open its mouth unprompted.

## Mechanics

Two pieces:

- **Triggers.** Events worth reacting to: a person arrives / leaves ([[presence]]),
  a long silence with someone present, a self-state change ([[self-model]] — low
  battery, just got unstuck), a time/ambient cue.
- **Gate + speak.** A policy decides whether a trigger actually becomes speech
  (don't greet the same person every 30 seconds; stay quiet when asked), then runs
  a turn through the existing pipeline so personality and tools still apply.

## Brainstorm — directions

- **Trigger registry.** A small set of named triggers (arrival, departure, idle,
  self-state, scheduled) feeding one gate, rather than scattered special cases.
- **Rate limiting / cooldowns.** Per-trigger and per-person cooldowns so initiative
  doesn't become pestering — greet Sam once per arrival, not per detection flap.
- **Quiet modes.** A "do not initiate" state (explicit, or inferred from "leave me
  alone") that suppresses proactive speech without disabling responses.
- **Reuse the turn pipeline.** A proactive utterance is a normal LLM turn with a
  system note ("Sam just walked in; greet them, briefly"), so personality, voice,
  and tools come for free.
- **Initiative budget.** Cap unprompted utterances per unit time so the robot has a
  felt sense of restraint.
- **Address the right person.** Pair with [[multi-person-conversation]] /
  `face_me` so a proactive greeting turns to and names the person it's for.
- **Context-aware suppression.** Don't interject into an ongoing human-to-human
  conversation the robot isn't part of.

## Open questions

1. **What earns speech?** Which triggers are worth interrupting silence, and which
   are merely logged?
2. **Cadence.** How often is too often? Global budget, per-trigger cooldown,
   per-person cooldown, or all three?
3. **Interruption etiquette.** May the robot speak while people are mid-conversation
   with each other, or only address it / silence?
4. **Off switch.** How is "be quiet" expressed and how long does it last?
5. **Trigger ownership.** Does the proactive layer subscribe to [[presence]] /
   [[self-model]] telemetry, or do those push events to it?
6. **Failure mode.** If a trigger fires while the robot is busy (mid-turn, moving),
   does the utterance queue, drop, or wait?

## Relationship to existing code

The proactive layer sits beside the wake-word path in the voice service: same turn
pipeline, different entry point. It consumes [[presence]] and [[self-model]] events
and is bounded by its own gate. The reactive wake-word flow is unchanged.
