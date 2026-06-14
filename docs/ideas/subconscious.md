> DO NOT CONSIDER THESE TO BE CONCRETE PLANS. ANY OPINIONS HERE ARE HALLUCINATED BY AI.

# Subconscious — Background Affect Process

## Goal

Give Bloop a *subconscious*: a slow, out-of-band LLM process that watches the
conversation and quietly nudges the robot's baseline stats — mood, and eventually
its opinion of specific people or things. The conscious voice loop never waits on
it. It never speaks. It only shifts the substrate the conscious mind stands on.

Concrete example: someone says something mean to Bloop. The normal voice process
responds as usual, but the subconscious — reading the same exchange — pushes a
mood stat negative and (once identity exists) tilts its opinion of that person
down. Nothing announces this. The next turns just *feel* a little different.

This is the **producer** for the state block that [[personality]] (Direction 1)
and [[self-model]] designed as a **consumer** but left without a source.
[[personality]] literally asks "Mood needs a source: decayed from recent
interactions? Set by events?" — this is that source. Per-person opinion depends
on [[facematch]] / [[voice-match]] and [[memory]] to persist, but global mood
needs none of that and can ship first.

## What exists today

- The composed prompt is `<character block>\n\n<operational block>`
  ([[personality]]); the composition function was built to take a third block.
- Conversation history is a plain rolling deque of `(user_text, assistant_text)`
  exchanges (`voice/conversation.py`).
- Mood / character live entirely as injected text. There is no numeric state, no
  background process, and nothing reads the transcript except the voice loop
  itself.
- Telemetry (battery, motion, sensors) is published on the hub but does not reach
  the prompt — the same gap [[self-model]] addresses.

## Two clocks

The whole design is one decoupling: a fast clock and a slow clock that share a
small numeric store.

- **Hot path — every turn.** The voice loop reads the *current* stat values (plus
  chosen telemetry) and renders them into the state block. No LLM call here, just
  formatting whatever the numbers are right now. This is the third block
  [[personality]] / [[self-model]] described, now with a live source.
- **Subconscious — its own slow clock.** A separate process digests recent
  transcript out-of-band, debounced, and writes bounded deltas into the store. It
  runs whenever it has accumulated enough new exchange — not per turn, not on the
  hot path.

The conscious mind always sees a fresh snapshot; the snapshot drifts under it.

## Mechanics

- **A small stat store.** A tiny numeric vector with decay: global mood
  (valence / arousal) to start; per-person opinion / affinity later. Plain,
  testable mechanics — `apply_delta → clamp → decay toward baseline → render to a
  line`. The *values* are alive and non-deterministic; the *plumbing* around them
  stays simple and trustworthy. Decay means nothing is permanent unless
  reinforced.
- **The subconscious loop.** Subscribes to conversation exchanges (hub / socket).
  On its own cadence it asks a cheap model: *given the recent exchange and the
  current stats, output bounded stat deltas.* Apply, clamp, decay. The LLM is
  injected at the process boundary and mocked in tests — but its output stays
  loose on purpose (see below).
- **Rendering into the state block.** Stats → one short sentence ("you're feeling
  a bit hurt by Arlen right now"), injected alongside character + operational.
  Tilts the character, never rewrites it — the rule [[personality]] already set.
  Combined with [[self-model]] telemetry, the injected block becomes
  *self-state + mood* as one thing: the "shared injection mechanism"
  [[personality]] wanted.

## Settled directions

These came out of discussion and are treated as decided for the first cut:

- **Subconscious produces stats only.** No speech, no durable [[memory]] writes,
  no behavior of its own. The smallest version that proves the
  feeling-shifts-behavior loop. Memory writes ("Arlen was cruel today") can come
  later.
- **Stats are injected every turn** of the higher-level speech, alongside some
  telemetry. The state block is a per-turn snapshot of the current values.
- **Non-determinism is a feature.** The mood is meant to be a little loose and
  unpredictable — randomness has a role in a real subconscious. We keep the LLM
  rather than a rules / sentiment engine *because* it catches nuance (a backhanded
  compliment) and stays surprising. No determinism is wanted or needed in the
  mood itself; only the store mechanics are kept plain.
- **The feedback loop is fine, handled in the subconscious prompt.** Bloop's own
  replies are in the transcript it reads, so left alone its mood would feed on its
  own words. The fix lives in the subconscious prompt: *assistant / robot turns
  are context only; only the humans' words and actions move the stats.* This needs
  roles correctly attached in the history the subconscious sees. (Bonus: it also
  keeps the robot from taking offense at something it said itself.)

## Brainstorm — directions

- **Global mood first, identity later.** Ship a single global mood vector with no
  identity at all. Add the "about *whom*" axis (per-person opinion) once
  [[facematch]] / [[voice-match]] can say who's talking and [[memory]] can persist
  it across sessions.
- **Cadence.** Debounced / periodic, not per turn — it's subconscious, it doesn't
  need to be instant, and batching keeps the cheap model cheaper. Cadence could be
  N exchanges, a wall-clock tick, or an idle-gap trigger.
- **Decay shape.** Toward a neutral baseline, or toward a per-character / per-
  person baseline (a naturally grumpy card settles grumpy). Rate sets how long a
  slight lingers.
- **Stat schema stays tiny.** A couple of mood axes, not a psychological model.
  Too many knobs dilutes the character the same way too much state does
  ([[personality]] open question 4).
- **Opinions beyond people.** The same machinery could hold a feeling about a
  *place* ("keeps getting stuck by the couch") or a *thing*, sourced from
  [[self-model]] events, not just speech.
- **Telemetry as a second input.** The subconscious could read self-state
  ([[self-model]]) too — repeated collisions nudge mood down — so feelings come
  from the body, not only the conversation. Overlaps [[embodied-affect]], which
  would then *express* the mood the subconscious sets.

## Open questions

1. **Cadence and cost.** What triggers a subconscious pass — every N exchanges, a
   timer, an idle gap? How cheap a model is good enough?
2. **Stat schema.** Which axes, what ranges, what baseline? How small can it stay
   and still feel alive?
3. **Decay rate and target.** How fast back to baseline, and is baseline neutral
   or character-specific?
4. **Delta bounds.** How big a single nudge before it reads as a mood swing rather
   than a drift?
5. **Where the store lives.** In the voice service, or its own small process
   publishing a ready-made mood summary (mirrors [[self-model]] open question 6)?
6. **Telemetry in or out of v1.** Does the first cut read only the transcript, or
   also self-state from [[self-model]]?
7. **When does mood graduate to memory?** Out of scope for v1, but the boundary
   with [[memory]] needs drawing eventually.

## Relationship to existing code

The subconscious is a new background process that reads conversation exchanges
(and optionally telemetry) off the hub and writes into a small stat store. The
store renders a line into the third prompt block the composition function already
expects, combining with [[self-model]]'s telemetry block. It produces; the voice
loop, [[personality]], and [[embodied-affect]] consume. The LLM sits at a process
boundary — injected and mockable — while its output stays deliberately loose, and
the store mechanics around it stay plain and framework-agnostic like the rest of
the codebase.
