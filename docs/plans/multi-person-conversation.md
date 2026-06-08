> DO NOT CONSIDER THESE TO BE CONCRETE PLANS. ANY OPINIONS HERE ARE HALLUCINATED BY AI.

# Multi-Person Conversation Plan

## Goal

Let Bloop hold a three-way (or multi-way) conversation and keep track of who
said what. Speech going into the LLM gets tagged by speaker — `Arlen:` ... /
`Sam:` ... — so the model knows there are several participants and can address
them individually.

Builds directly on [[voice-match]]: voice matching answers "who is speaking,"
this turns that into a labeled, multi-party transcript the LLM reasons over.

## What exists today

The conversation history (`src/voice/conversation.py`) models one user and one
assistant — every incoming turn is an anonymous "user." There is no notion of
multiple distinct speakers in a single session.

## Mechanics

Two pieces, both downstream of speaker identity:

- **Label the input.** When [[voice-match]] (or DoA / face) identifies the
  speaker of an utterance, prefix the transcript with their name before it goes
  into the LLM context: `Arlen: where did you put it?`. Unknown speakers get a
  stable placeholder (`Speaker 2:`).
- **Carry participants in the prompt.** Tell the model who is present and that it's
  a group conversation, so it can name people and follow turn-taking instead of
  treating every line as the same person.

## Candidate shapes

### A. Inline name prefixes

Each user message in `conversation.py` carries the speaker name inline in the
text (`Name: utterance`). Minimal change to the history model; the LLM infers
participants from the prefixes.

### B. Structured speaker field

Conversation turns gain an explicit `speaker` field (id + display name) alongside
the text. The prompt builder renders prefixes from it, and the field is also
available to memory/personality without re-parsing text.

### C. Roster block

A maintained "who's in the room" list (joined / left / last spoke) injected into
the prompt as its own block, on top of A or B. The model gets the current
participant set explicitly rather than reconstructing it from history.

## Brainstorm — directions

- **Stable session-local ids.** Even before a name is known, give each distinct
  voice a consistent handle for the session (`Speaker 2:`) so the model can track
  them; upgrade the handle to a real name if [[voice-match]] resolves it later.
- **Backfill on late identification.** If a speaker is identified a few turns in,
  relabel their earlier `Speaker N:` lines to the real name in the history.
- **Fuse signals for attribution.** Use DoA direction (`src/voice/doa.py`) and
  [[facematch]] to attribute a turn when voice alone is ambiguous — direction
  changes are a strong "different person now" cue.
- **Addressee, not just speaker.** Track who Bloop is replying *to*, so "face me"
  / gaze / DoA targeting points at the right participant.
- **Barge-in / overlap handling.** Decide what happens when two people talk over
  each other — split into two labeled turns, or drop the overlapped segment.
- **Per-speaker memory routing.** A tagged transcript lets [[memory]] attribute
  facts to the right person automatically ("Sam said he hates the vacuum").
- **Roster lifecycle.** Add a participant when a new voice/face appears; mark them
  gone after a period of silence/absence so the roster reflects who's actually
  present.

## Open questions

1. **History model.** Inline prefixes (A) or a structured `speaker` field (B) in
   `conversation.py`? Does the LLM see one merged stream, or per-speaker turns?
2. **Unknown speakers.** How are unidentified voices labeled and kept stable
   within a session, and do those placeholders persist across sessions?
3. **Attribution source.** Voice match alone, or fused with DoA / face — and what's
   the fallback when they disagree?
4. **Turn boundaries.** With multiple talkers, what defines one "turn" for the LLM
   — silence gaps, speaker changes, both?
5. **Addressing.** Should the model track and express *who* it's talking to (gaze,
   `face_me`, naming), or just produce one reply to the group?
6. **Roster surfacing.** Expose the current participant list in telemetry / the
   dashboard, and is it shared with [[voice-match]] / [[facematch]]?

## Relationship to existing code

`src/voice/conversation.py` is the natural home for speaker-aware turns. The
identity itself comes from [[voice-match]] (and optionally [[facematch]] / DoA);
this plan is about representing and using that identity inside the conversation,
not producing it.
