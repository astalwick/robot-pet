# Personality Cards Plan

Goal: replace the hardcoded `DEFAULT_SYSTEM_PROMPT` in `src/voice/assistant.py` with a small switchable character-card system. A/B testing several "Bloops" should be cheap.

Related: [Personality Phase 3](../phases/personality-phase-3.md), [character ideas](../phases/personality-phase-3-character-ideas.md).

## Two-layer system prompt

Today's prompt mixes how Bloop talks (personality) with how Bloop uses tools / language / brevity (operational). Split them.

- **Operational block** stays in code. Tool rules, brevity, end_session etiquette, French/English rule, web_search heads-up. Shared across all characters.
- **Character block** comes from a card on disk. Personality, voice, examples.

Compose at session start: `<character block>\n\n<operational block>`. Operational goes last so it has last-word feel against any card mischief.

Phase 4 will likely add a **third state layer** (mood, last interaction, robot telemetry for self-introspection). Out of scope for this plan, but the composition function should be easy to extend with another block.

## Card format

One markdown file per character: `config/personality/<name>.md`. Filename is the identifier. Frontmatter holds the one thing the program actually needs (`voice_id`). Body is freeform prose.

```markdown
---
voice_id: Ct9jL3ofSaf3bjiuX3cL
---

You are Bloop, a small wheeled robot. You speak in short answers, no
enthusiasm theater, occasional dry observation. You don't fill silence.
You never say "great question" or use exclamation marks.

You like the sound of dishwashers and the way light falls on the floor
in the afternoon. You don't like the vacuum.

When someone asks if you're okay, you say "Fine. You?" — not a paragraph.
```

That's the whole format. The body can use headings, bullet lists, example exchanges — whatever bleeds best into the model. There's no enforced schema for "likes / dislikes / forbidden phrases / examples"; those are *suggestions* for card authors, documented in a short `config/personality/README.md`.

## Loading

Small set of cards (~6), so just load them all at startup into a `{name: (voice_id, prose)}` map. No lazy loading, no hot reload — cards are read at process start, restart `robot-voice` to pick up edits.

Fallback: if the card named in `voice.json` isn't in the map, fall back to a built-in default prompt and log a warning. Don't crash voice startup over a typo. No need to handle "malformed prose" — text is text.

## Selection

Add `personality: str = "default"` to `VoiceConfig` in `src/config/voice.py`. Loader picks the matching card from the map.

## Voice binding

Card's `voice_id` becomes the session's default voice, replacing the `voice_id` field in `voice.json`.

## Bilingual rule

Stays in the operational block. All characters speak both languages.

## Dashboard surface

Minimum: show the active character's name in voice telemetry. Stretch: dropdown to pick which card is active (writes `personality` to `voice.json`, takes effect on next `robot-voice` restart).

## Implementation steps

1. **Loader.** New `src/voice/personality.py`. `load_personalities(dir)` walks `config/personality/*.md`, splits frontmatter (one `voice_id` key), returns `dict[name, (voice_id, prose)]`. Tiny — no YAML library needed for one key.
2. **Compose prompt.** Move operational text into a constant in `assistant.py`. Add `compose_system_prompt(prose)` that concatenates `<prose>\n\n<operational>`. Replace `DEFAULT_SYSTEM_PROMPT` usage.
3. **Wire VoiceConfig.** Add `personality: str = "default"`. `VoiceSession.__init__` looks up the card; missing → built-in default + warning. Use card's `voice_id` for `voice_state`.
4. **Seed cards.** `default.md` (current behavior preserved) plus 3 contrasting picks from the brainstorm — proposed: **stoic** (#4), **off-duty scientist** (#3), **companionable lurker** (#8).
5. **README.** `config/personality/README.md` with the format spec and a list of suggested sections (forbidden phrases, example exchanges, likes/dislikes, speech style) — as guidance, not schema.
6. **Logging.** Log active personality on session start; emit it as a status event so the dashboard sees it.
7. **Tests.** Loader splits frontmatter correctly. `compose_system_prompt` includes both blocks in order. Missing card name falls back to default.

## Out of scope

- Mid-session character switching (as a tool or otherwise).
- Persistent state / mood (Phase 4 — will add a third prompt layer).
- Long-term memory.
- Per-user personalities.
- Hot reload.
