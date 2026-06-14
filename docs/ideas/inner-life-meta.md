> DO NOT CONSIDER THESE TO BE CONCRETE PLANS. ANY OPINIONS HERE ARE HALLUCINATED BY AI.

# Meta — thinking about inner life, subconscious, and mood

A commentary note, not a plan. It reacts to [[subconscious]], [[self-model]], and
[[personality]] (Direction 1) as a cluster — the "inner life" layers — and
collects refinements, tensions, and directions worth chasing. Each linked doc
still owns its own design and open questions; this is the layer of opinion on top.

## What's already right

Putting the LLM in the *subconscious* — slow, off the turn loop — and keeping the
hot path a dumb render is correct. The strongest argument in [[subconscious]] is
that an LLM catches a *backhanded* compliment where a sentiment library can't, and
that's worth the cost precisely because it runs off-path. Keep that.

## Two timescales, not one

[[subconscious]] is described as one slow process, but its examples smuggle in two
different things:

- **"hurt by Arlen," "recoil on a bump"** — fast, object-directed, short. That's
  *emotion*.
- **"the next turns just feel a little different"** — slow, diffuse, objectless.
  That's *mood*.

The split maps onto the architecture:

- **Fast affect** = a reflex off [[self-model]] events (bump → startle, unstuck →
  relief). No LLM round-trip; can drive [[embodied-affect]] reflexively. This is
  the "reflexive vs. deliberate" line those docs already gesture at.
- **Slow mood** = the [[subconscious]] digest, the baseline that drifts under
  everything.

Same prompt block, two producers, two clocks. Today one slow clock is being asked
to do both jobs.

## Divide the axes by their natural source

[[self-model]] and [[subconscious]] both write the state block, but the labor is
never divided. Divide it by where the signal honestly lives:

- **[[self-model]] owns arousal / energy** — nearly free from battery + clock +
  recent motion ("sleepy at night," "low-energy on low battery," "wound up after a
  chase"). Bodily arousal from bodily telemetry.
- **[[subconscious]] owns valence and (later) per-person opinion** — the social
  axis that actually needs an LLM reading the transcript.

That's a two-axis mood in v1 for almost no extra cost, each axis sourced from the
thing that legitimately knows it.

Consider **PAD (pleasure-arousal-dominance), not just valence-arousal.** The
dominance/control axis is the one a *robot body* expresses best — "stuck, can't
move" is a control hit, not a valence hit, and "slumped vs. upright" is dominance
made physical. It's the axis the idea docs circle (stuck, can't-do, capability
awareness) without naming.

## The number↔text sandwich

As written: LLM reads transcript → numeric delta → store decays → renders a
sentence. Two lossy text↔number conversions wrap a scalar, and the nuance the LLM
caught is gone by the time "valence −0.3" becomes words again.

Keep the number for mechanics (decay, clamp, bounded deltas, a dial for
embodiment, testable plumbing), but have the subconscious emit **(delta, cause)** —
a magnitude *and* a short cause-string — and carry the cause through. Render =
magnitude drives intensity, cause supplies specificity: "a bit rattled — Arlen
snapped at you." You keep the plain numeric store *and* the reason the LLM was
worth using.

## Show, don't tell

Rendering mood as an explicit sentence makes it fully introspectable, so the robot
tends to *announce* it ("sorry, I'm a bit out of it"). A real subconscious is the
opposite of introspectable. Phrase the injected line as an instruction to
*expression*, not a *report* of state: "let this color your tone; do not name it."
Otherwise every mood becomes something the robot talks about — needy, not moody.

## Keep the robot's own turns in the transcript (corrected)

An earlier version of this note argued for *stripping* the robot's turns from what
the subconscious reads, to kill the self-reinforcing loop structurally. **That's
wrong.** A one-sided transcript is hard to understand, and the backhanded
compliment *depends on* seeing what the robot said to be recognized as backhanded.
Remove the robot's side and you remove the context that makes the human's side
legible.

So: keep both sides in the history with **roles correctly attached**, and keep the
guard where [[subconscious]] already put it — in the prompt: *robot turns are
context only; only the humans' words and actions move the stats.* This leans on the
cheap model following an instruction, which may or may not hold. The plan is to
**watch it fail before engineering around it** — see the failure, then decide,
rather than pre-solving a problem we haven't observed.

(The loop worth *keeping*, either way: mood → replies → humans react → mood. That's
social dynamics, the good kind.)

## A character *is* its mood dynamics

The cleanest interlock between [[personality]] and mood, currently buried as a
brainstorm bullet: a character isn't only prose — it's a set of
**(baseline, gain, decay) per axis**.

- baseline → grumpy card settles grumpy, sunny card settles sunny
- decay rate → resilience (how long a slight lingers)
- delta gain → sensitivity (how hard the same insult lands)

Same event, different character, different felt result — no per-character mood
logic. The card stays prose identity; three numbers per axis give it temperament.
This also answers "where does mood live relative to character": they're the same
object.

## Two new directions

- **Homeostatic drives, not just reactive mood.** Everything here is reactive —
  things happen, mood moves. Living things also have drives that build on their
  own: boredom, curiosity, social hunger. A boredom drive rising with idle time is
  a better engine for [[proactive-behavior]] than a timer — the robot speaks up
  because it's *bored*, not because 90 seconds passed. Battery is already a real
  homeostatic variable. Reframes initiative as *drive satisfaction* rather than
  *trigger response*. Probably the most interesting missing piece.
- **A sleep / consolidation pass.** The subconscious only runs during conversation,
  but a subconscious does its best work offline. An idle/overnight pass that
  consolidates the day's transcript into [[memory]], settles mood toward baseline,
  and lets per-person opinion graduate from transient mood into durable fact —
  one mechanism that closes both [[memory]]'s "what becomes durable" and
  [[subconscious]]'s "when does mood graduate to memory."

## Smaller gaps

- **Mood across reboot.** If the store is in-memory the robot is born neutral every
  boot. Persist it and **decay by wall-clock elapsed while off** — back after an
  hour, the slight lingers; after a week, fully settled. Most of the "continuous
  being" illusion for very little.
- **The interesting part isn't unit-testable.** Mocking the LLM tests
  apply→clamp→decay (plumbing), not "does a sneer lower mood" (the point). Plan a
  small *scenario eval* — transcript fixtures asserting delta sign/magnitude bands
  against the real cheap model — separate from unit tests.
- **Two decay models will collide.** Mood decays toward baseline; [[memory]] ages
  by `last_seen`. When opinion crosses between them, one story is needed for how a
  strong recent interaction overrides a stale stored opinion — same shape as
  memory's contradiction handling.

## In one sentence

The current design treats mood as a single reactive scalar rendered as a
confession; it'd be more alive as two axes (bodily + social) over two timescales
(reflex + drift), sourced from where the signal actually lives, expressed rather
than announced, with drives pushing and sleep consolidating.
