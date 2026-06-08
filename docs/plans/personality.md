> DO NOT CONSIDER THESE TO BE CONCRETE PLANS. ANY OPINIONS HERE ARE HALLUCINATED BY AI.

# Personality — Additional Directions

The base character-card system shipped (see
[done/2026-05-27 - personality-cards.md](done/2026-05-27%20-%20personality-cards.md)):
cards live in `config/personality/*.md`, the operational block is split out into
`config/operational_system_prompt.md`, and `VoiceConfig.personality` selects one.
This doc collects directions for personality beyond that.

## What the base system deferred

The cards plan listed these as out of scope:

- Persistent state / mood ("Phase 4 — third prompt layer").
- Long-term memory.
- Per-user personalities.

## Direction 1: the third prompt layer (mood / state)

Today the composed prompt is `<character block>\n\n<operational block>`. A third
**state block** could carry mood, last interaction, and robot self-introspection
(battery, what it just did, whether it's stuck — see [[self-model]]).

- The composition function was built to be extended with another block.
- Self-introspection pulls from telemetry the robot already publishes (battery,
  motion, sensors), so "I'm low on battery" reflects real state.
- Mood needs a source: decayed from recent interactions? Set by events (bumped
  something, got ignored, got praised)?

This layer is where personality and [[memory]] meet — a memory block ("you talked
about X yesterday") is the same kind of injected context as a mood block.

## Direction 2: per-user personality / relationships

Once the robot knows *who* it's talking to (via [[facematch]] or [[voice-match]]):

- Greet and treat known people differently.
- Hold a per-person relationship state (familiarity, inside jokes, running bits)
  that feeds the state block.

Depends on identity.

## Direction 3: more cards

Grow beyond the seeded set. The format is freeform prose.

## Brainstorm — directions

- **Separate stable traits from transient state.** The character card stays the
  fixed identity; the state block carries only what changes (mood, last
  interaction). Mood never rewrites the character, it tilts it.
- **Event-driven mood.** Update a small mood scalar/vector from tagged events:
  collision / stuck (motion telemetry), long silence (interaction cadence), praise
  or scolding (sentiment of the user's words). Decay back toward neutral over time.
- **Mood as an injected line, not a rewrite.** Render the current state as a short
  sentence ("you're a bit rattled — you just bumped the table") appended to the
  prompt, leaving the character block untouched.
- **Ambient/context block.** A clock/sensor-derived line (time of day, ambient
  light) the character can reference, matching the seed card's "afternoon light."
- **Per-person relationship notes.** Compact per-person state (familiarity, running
  bits) feeding the state block, sourced from identity ([[facematch]] /
  [[voice-match]]) and durable facts ([[memory]]).
- **Shared injection mechanism.** Mood, ambient, memory recall, and relationship
  notes are all the same kind of injected context — one extensible state block
  rather than separate prompt plumbing per feature.

## Open questions

1. **Where does mood live and how does it change?**
2. **Truthful self-introspection vs. character.** When telemetry says "battery
   low," how much does each character bend that into its own voice without
   contradicting the underlying fact?
3. **Ordering of blocks.** Operational currently goes last. Where does the
   state/mood block go relative to the character block?
4. **How much state is too much** before it dilutes the character?
