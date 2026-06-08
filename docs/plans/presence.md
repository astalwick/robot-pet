> DO NOT CONSIDER THESE TO BE CONCRETE PLANS. ANY OPINIONS HERE ARE HALLUCINATED BY AI.

# Presence / Social Awareness Plan

## Goal

Let Bloop know who is *around* — not just who is mid-conversation. Today
everything starts from a wake word; between turns the robot is socially blind. A
presence tracker maintains a running sense of who is present, where, and for how
long, even when no one is talking to it.

This is the precondition for [[proactive-behavior]] (you can't greet someone
arriving if you don't notice them arrive) and a source for [[personality]] and
[[memory]].

## What exists today

- `robot-vision` detects faces and publishes normalized boxes through telemetry.
- `src/voice/doa.py` gives direction of arrival for sound.
- Identity comes from [[facematch]] / [[voice-match]] via [[identity]].

Each is a momentary signal. Nothing maintains "who is in the room over time."

## Mechanics

A presence tracker fuses those signals into a set of present people:

- **Detect.** Face boxes (vision) and sound direction (DoA) say "something/someone
  is over there."
- **Identify.** [[facematch]] / [[voice-match]] resolve a detection to a person id
  in [[identity]] when possible; otherwise a stable placeholder.
- **Track over time.** Maintain per-person state: present / absent, last seen,
  rough direction, time in room. Entries decay to "gone" after silence/absence.

The output is a small "who's here" set published on telemetry, consumed by the
conversation, personality, and proactive layers.

## Brainstorm — directions

- **Arrival / departure events.** Emit "person entered" / "person left" rather
  than only a current snapshot, so downstream behavior can react to the change.
- **Confidence over presence, not just identity.** Track "someone is here but
  unidentified" distinctly from "Arlen is here" and from "empty room."
- **Direction without recognition.** DoA + detection can place a person spatially
  even before identity resolves — enough to turn toward them.
- **Hysteresis.** Require a few consistent frames/sounds before declaring arrival
  or departure, so a glance away or a quiet moment doesn't flap the roster.
- **Feeds the conversation roster.** The present set seeds
  [[multi-person-conversation]]'s participant list.
- **Cheap idle loop.** Presence runs continuously at low rate; expensive
  recognition fires only on change (new/unresolved detection), bounding cost.

## Open questions

1. **Fusion.** How are vision and DoA combined into one person entry, and what
   happens when they disagree on count or location?
2. **Decay timing.** How long absent/silent before someone is "gone"? Different
   for someone who walked out vs. someone sitting quietly?
3. **Granularity.** Just a set of present people, or positions/zones in the room?
4. **Cost.** What runs every tick (cheap detection) vs. on change (recognition),
   and at what rate?
5. **Ownership.** Is presence part of `robot-vision`, the voice service, or its
   own small service consuming both telemetry streams?
6. **Telemetry shape.** One "who's here" stream shared by conversation /
   personality / proactive, or per-consumer?

## Relationship to existing code

Presence sits above the momentary signals — `robot-vision` boxes,
`src/voice/doa.py`, and the [[identity]] lookups behind [[facematch]] /
[[voice-match]] — and turns them into durable per-person state. The detectors stay
momentary and pure; presence owns the time dimension.
