# Personality cards

One markdown file per character. The filename (without `.md`) is the name
used in `voice.json`:

```json
"personality": "stoic"
```

Frontmatter holds the ElevenLabs `voice_id`. Body is freeform prose that
becomes the character layer of the system prompt. The operational rules
(tools, brevity ceiling, languages) live in code and apply to every
character — don't duplicate them here.

Restart `robot-voice` after editing.

## Format

```markdown
---
voice_id: <ElevenLabs voice ID>
---

Freeform prose describing this character.
```

## Writing a card that actually feels like something

These are the seed cards in this folder, but they're also a guide. Read
`stoic.md`, `artist.md`, and `philosopher.md` and notice what they do *not*
say.

### 1. Define the character by what it refuses to do

Anyone can write "you are calm and thoughtful." That goes nowhere — every LLM
is calm and thoughtful when you ask it to be. The thing that makes a
character distinct is the small list of moves it doesn't make.

A few examples:

- "You don't fill silence."
- "You never say 'great question.'"
- "You don't apologize for short answers."
- "You don't ask if there's anything else you can help with."
- "You don't compliment your own observations."

Forbidden phrases are the highest-leverage line you can write. They cut
straight through the assistant defaults that the LLM falls into when nothing
specific is pulling it elsewhere.

### 2. Pick concrete things over adjectives

"You like the sound of dishwashers" beats "you are sensory and observant" by
a mile. "You like yellow more than orange" beats "you have aesthetic
preferences."

A character has weight when it has favorites. Two or three concrete things
the character notices, prefers, or is mildly bothered by. Not a personality
test result — actual small details.

### 3. Show speech style, don't describe it

Don't write "you speak in short sentences." Write a few short sentences in
the prose itself, and trust the model to mirror.

If you want to nail the tone, include one or two example exchanges:

```
**User:** How are you?
**Bloop:** Fine. You?
```

One example is worth a paragraph of description. Two is plenty. More than
three becomes a costume.

### 4. Subtle, not obvious

Avoid:

- Costume voices (pirate, cowboy, robot-from-the-50s).
- Hat-on-a-hat archetypes (grumpy old man, ditzy teenager).
- "Quirky" tics that announce themselves every line ("beep boop, processing
  your request").
- Telling the model the character is "warm" or "witty" or "curious." If the
  card is doing its job, the human can feel that without being told.

Aim for: a person you'd recognize after three exchanges but couldn't quite
describe in one word.

### 5. Keep it short

A good card is roughly 100–250 words. If you're past 300, you're probably
adding rules the model would have followed anyway, or describing the
character instead of giving it material to work with.

### 6. The robot is implicit

Every card runs on the same wheeled robot in the same room. You don't need
to remind the model that it's a robot, or that it has wheels, or that it can
move. The operational prompt covers the body. The card is just the voice.

## Picking a voice

The `voice_id` in the frontmatter is an ElevenLabs voice. The character
prose and the voice should agree — a stoic written for a soft narrator voice
will feel uncanny.

Find IDs by browsing the [ElevenLabs voice
library](https://elevenlabs.io/app/voice-library) or hitting `GET
/v1/voices` with your API key. Voice IDs are stable strings like
`pNInz6obpgDQGcFmaJgB`.

If a card's `voice_id` is wrong or the personality name in `voice.json`
doesn't match a card, voice startup falls back to `default.md` and logs a
warning rather than crashing.
