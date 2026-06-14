> DO NOT CONSIDER THESE TO BE CONCRETE PLANS. ANY OPINIONS HERE ARE HALLUCINATED BY AI.

# Sequencing — what to build, in what order

A suggested build order for the ideas in this folder. The ideas cross-reference
each other heavily; this note reads that dependency graph and turns it into a
sequence. It is about *ordering*, not committing to any one idea's design — each
linked doc still owns its own open questions.

## The two keystones

Almost everything here hangs off one of two foundations. Build these and the rest
becomes incremental.

- **The third prompt block** — the "state layer" [[personality]] deferred.
  [[self-model]] and [[subconscious]] both *write* it; [[personality]] and
  [[embodied-affect]] *read* it. Nothing here needs new perception — it is built
  from telemetry and conversation that already exist.
- **The identity registry** ([[identity]]) — [[facematch]], [[voice-match]],
  [[multi-person-conversation]], [[presence]], per-user [[personality]],
  per-person [[memory]], and per-person [[subconscious]] opinions all hang off it.

## The key sequencing lever

Several ideas split cleanly into a **global / self** version that needs no
identity and a **per-person** version that does. Mood, memory, and opinions all
do this. Ship the global version early; defer the per-person version until
[[identity]] exists. The docs say this themselves repeatedly.

## Recommended order

### Phase 1 — Inner life (no new sensors)

1. **[[self-model]]** — turn telemetry the robot already publishes into the third
   prompt block. Pure consumer of what exists, low risk; the concrete form of
   [[personality]]'s deferred state layer. The natural first move.
2. **[[subconscious]]** (global mood only) — the *source* for mood that
   [[personality]] explicitly left open. Needs the block from step 1 to exist.
   Together, 1 + 2 close [[personality]] Direction 1.

### Phase 2 — Persistence

3. **[[memory]]** (non-person facts: places, experiences, self-events) — survives
   restart, needs no identity. Also gives [[subconscious]] somewhere to graduate
   opinions to later.
4. **[[self-created-tools]]** (shape A/B, macros) — depends on [[memory]] for
   persistence; otherwise self-contained. Reuses the existing validated motion
   callers, so bounds and preemption come for free.

### Phase 3 — Identity foundation

5. **[[identity]]** (person registry) — build the shared substrate *before* the
   recognizers, so they have somewhere to write. Depends on nothing.
6. **First recognizer** — lead with **[[facematch]]** (option A, LLM offload:
   `robot-vision` already crops, ~10 people is a bounded prompt, no new
   model/service). [[voice-match]] is a fine alternative-first if camera-off
   recognition matters more.

### Phase 4 — Social awareness

7. **Second recognizer** ([[voice-match]] / [[facematch]]) — fusion gets more
   robust with both signals.
8. **[[multi-person-conversation]]** — directly downstream of [[voice-match]]
   (speaker-labeled transcript).
9. **[[presence]]** — fuses vision + DoA + identity into durable "who's here over
   time."
10. **Per-person upgrades**, now unlocked: per-user [[personality]], per-person
    [[memory]] attribution, per-person [[subconscious]] opinion. Extensions to
    things already shipped, not new builds.

### Phase 5 — Initiative & expression

11. **[[proactive-behavior]]** — needs [[presence]] (arrival/departure) and
    [[self-model]] events to react to.
12. **[[embodied-affect]]** — needs the mood state ([[subconscious]] /
    [[self-model]]) *and* the macro shape from [[self-created-tools]]; the body
    finally expressing what the inner-life layers compute. A fitting capstone.

## Judgment calls worth revisiting

- **Phases 1–2 vs. identity first.** Inner life is front-loaded because it needs
  zero new perception and delivers visible character immediately. If the real
  goal is "the robot knows who I am," pull [[identity]] + the first recognizer
  earlier — but then you are building perception plumbing before there is a
  personality to attach it to.
- **Which recognizer leads** (face vs. voice) — depends on whether
  across-the-room / lights-off recognition matters more than on-camera. Easy to
  defer.

## Not part of this arc

`archived or done/input.md` (wireless control inputs) is a separate hardware /
teleop track, unrelated to this personality-and-social sequence.
