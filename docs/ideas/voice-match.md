> DO NOT CONSIDER THESE TO BE CONCRETE PLANS. ANY OPINIONS HERE ARE HALLUCINATED BY AI.

# Voice Matching Plan

## Goal

Move from "someone said something" to "*Arlen* said something" — identify the
speaker from their voice. Counterpart to [[facematch]] (identity from vision);
the two can confirm or substitute for each other.

Voice matching would let the robot:

- Know who's talking even when no one is on camera (across the room, lights off).
- Attach memories and relationships to a person ([[memory]], [[personality]]).
- Disambiguate when several people are present — feeds
  [[multi-person-conversation]].

## What exists today

The voice stack transcribes with ElevenLabs Scribe (STT), runs the turn through
the OpenAI Responses LLM, and speaks with ElevenLabs Flash (TTS). `src/voice/doa.py`
gives **direction** of arrival — where a sound came from — but not **who**. There
is no speaker identity today; every voice is anonymous.

The household is small (~10 people), same bound as [[facematch]].

## Where does recognition run?

Speaker identification means turning a short utterance into a voice embedding and
matching it against a gallery of known voices. Placement options mirror facematch:

### A. On the Pi (small speaker model)

A compact speaker-embedding model (e.g. an ECAPA-TDNN / Resemblyzer-class model)
runs on the Pi, embeds the captured utterance, and matches against a local gallery.

- Self-contained; works with no network and no second machine.
- Adds a model and its runtime to the Pi; CPU cost per utterance.

### B. Offload to a Mac service

The Pi ships the captured audio clip; a service on the MacBook embeds and matches
against a local gallery, returns a name. Mirrors the dashboard-over-HTTP pattern.

- Heavier/better models on capable hardware.
- Only works when the Mac is reachable; another process to keep alive.

### C. Offload to a speaker-ID API

A hosted speaker-identification / verification service does the matching.

- No local model to maintain.
- Per-call cost and latency; audio leaves the network.

## Brainstorm — directions

- **Match once per utterance.** Identity is decided on the captured turn audio
  Scribe already buffers, not continuously — one embedding per spoken turn.
- **Reuse the captured clip.** Hook the same audio the STT path already records;
  avoid opening a second capture of the mic.
- **Verify vs identify.** Open-set identify ("which of N, or nobody") is harder
  than verify ("is this Arlen, yes/no"). A verify step can confirm a guess from
  another signal (DoA position, face) cheaply.
- **Fuse with DoA and face.** Combine [[facematch]] identity, DoA direction
  (`src/voice/doa.py`), and voice embedding — when two agree, confidence is high;
  when one is missing (off camera, silent), another carries.
- **Enroll by conversation.** "It's me, Sam" labels the current utterance's
  embedding — registration without a separate capture step.
- **Accumulate samples over time.** Keep several embeddings per person across
  sessions to cover phone-voice, tired-voice, distance; cap per person.
- **Confidence band.** Below a threshold, stay anonymous rather than guessing a
  wrong name.

## Open questions

1. **Placement.** On-Pi model (A), Mac service (B), or hosted API (C)?
2. **Identify vs verify.** Full open-set identification, or just verify a guess
   that DoA / face already proposed?
3. **Enrollment.** How does a new voice get registered — explicit "this is Sam,"
   or passively accumulated once another signal labels the speaker?
4. **Reference storage.** Where voice embeddings live (e.g. under
   `/home/pi/.config/robot-pet/`) and how they relate to facematch's gallery —
   one person record spanning face + voice, or separate?
5. **Robustness.** TTS playback, multiple talkers, and background noise corrupt the
   clip. Gate on DoA / barge-in state, or require a clean single-speaker window?
6. **Telemetry shape.** Publish speaker name/confidence per turn alongside the
   transcript, or as a separate identity stream shared with [[facematch]]?

## Relationship to existing code

DoA stays direction-only; voice matching is added alongside it, reading the same
turn audio the STT path captures. If recognition offloads (B/C), only short audio
clips leave the Pi, and only for identification — the framework-agnostic voice
drivers stay pure.
