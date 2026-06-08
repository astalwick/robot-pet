> DO NOT CONSIDER THESE TO BE CONCRETE PLANS. ANY OPINIONS HERE ARE HALLUCINATED BY AI.

# Embodied Affect / Expression Plan

## Goal

Let Bloop express state through its body, not just its words. Today mood and
character live entirely in text ([[personality]]); the robot's motion is task
motion (drive, look, `face_me`). This explores expressing affect through movement,
sound, and light — a happy wiggle, a dejected slump, a curious lean — so the
personality reaches the body.

Overlaps [[self-created-tools]] (the "little dance" is an expressive move) and
draws its state from [[personality]] / [[self-model]].

## What exists today

- Motion goes through the motion-intent socket to `robot-motion` (bounded,
  gamepad-preemptible). `wiggle` is already an expressive-ish move.
- Personality / mood is text injected into the prompt.
- No mapping from "feeling" to "gesture," and no non-motion output channels wired
  for expression (sound effects, light) beyond TTS speech.

## Mechanics

- **A vocabulary of expressive moves.** Named gestures (wiggle, lean-in, recoil,
  nod, slump) that are short, bounded motion sequences through the existing
  `robot-motion` path — the same shape as a [[self-created-tools]] macro.
- **A mapping from state to expression.** Mood / event ([[personality]],
  [[self-model]]) selects a gesture: praised → wiggle, bumped something → recoil.
- **Channels beyond motion.** If light / sound output exists or is added, the same
  state can drive a color or a chirp alongside or instead of motion.

## Brainstorm — directions

- **Gestures as bounded macros.** Reuse the [[self-created-tools]] macro shape so
  expressive moves inherit bounds, timeouts, and gamepad preemption for free.
- **Affect channel, separate from task motion.** Expression shouldn't fight a drive
  command — define how an expressive gesture yields to or layers under task motion.
- **LLM-selected vs. reflexive.** Some expression is the model choosing a gesture
  mid-turn (a tool call); some is reflexive (recoil on a bump) without an LLM round
  trip.
- **Idle/ambient motion.** Small life-like movement at rest (a look-around, a
  settle) so the robot reads as "on" rather than frozen — gated to stay quiet when
  appropriate.
- **Multimodal mood.** Drive light/sound from the same mood scalar that tilts the
  text ([[personality]]), so voice, words, and body agree.
- **Speech-synced motion.** Small movement timed to TTS playback (a nod on
  emphasis) so talking looks embodied.

## Open questions

1. **Channels.** Motion only at first, or is there light / sound hardware to drive?
2. **Arbitration.** When expression and task motion both want the body, which wins,
   and does expression layer or interrupt?
3. **Reflexive vs. deliberate.** Which expressions bypass the LLM (fast reflex) and
   which are model-chosen?
4. **Source of affect.** Does expression read the same mood state as
   [[personality]] / [[self-model]], or get chosen per-utterance by the LLM?
5. **Idle behavior.** Should the robot move at rest, and how is that suppressed
   when it should hold still?
6. **Authoring.** Are expressive moves built-in, or definable like
   [[self-created-tools]] macros?

## Relationship to existing code

Expressive motion rides the existing motion-intent socket to `robot-motion`, so
it stays inside the motion boundary. Gestures share the macro shape from
[[self-created-tools]]; the state that selects them comes from [[personality]] /
[[self-model]]. New output channels (light/sound) would be new drivers, kept
framework-agnostic like the rest.
