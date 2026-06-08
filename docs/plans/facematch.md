> DO NOT CONSIDER THESE TO BE CONCRETE PLANS. ANY OPINIONS HERE ARE HALLUCINATED BY AI.

# Face Matching Plan

## Goal

Move from "there is a face" to "this is *Arlen's* face." Today `robot-vision`
(`src/robot_vision.py`) runs OpenCV Haar detection on the Pi CPU and publishes
normalized face boxes through the telemetry hub. It detects but does not
recognize.

Face matching would let the robot:

- Greet known people by name.
- Attach memories to a person (ties into [[memory]]).
- Pick a personality or behavior per person (ties into [[personality]]).

## Where does recognition run?

Recognition does **not** run on the Pi. Detection stays on-Pi (`robot-vision`
already does it); the Pi crops the face and offloads the "who is this?" decision.
The household is small — roughly ten people, max. Two placements, plus a maybe.

### A. Offload to an LLM directly

`robot-vision` crops the face; ask a vision-capable LLM "which of these known
people is this, if any?" with a handful of reference photos per person inline.
With ~10 people this is a bounded prompt.

- No embedding model, no gallery, no second service — a prompt and reference
  images. Uses the existing voice-stack LLM access.
- The model handles lighting/angle variation itself.
- Latency and per-call cost; not for video-rate use. Depends on network
  reachability to the LLM.

### B. Offload to a Mac service

`robot-vision` crops the face; a service on the MacBook embeds it and matches
against a local gallery, returns a name. Mirrors the existing pattern where the
dashboard MacBook talks to the Pi over HTTP.

- Fast repeated matching once the gallery exists; runs local on capable hardware.
- Only works when the Mac is reachable. Adds a process to keep alive.

### C. Hybrid (maybe — probably not)

Use the LLM for occasional ground-truth labeling and the Mac service for fast
runtime matching, or fall back between them by availability. Likely not worth the
coordination for ~10 people; listed for completeness.

In all cases: detection stays on the Pi. Only cropped face images leave the Pi,
and only for recognition.

## Brainstorm — directions

- **Recognize per track, not per frame.** Run recognition once when a new face
  track appears, label the track, and let the label stick while the box is
  followed. Reuses the box tracking `robot-vision` already does and bounds how
  often the offload fires.
- **Trigger on event, not continuously.** Run recognition on wake word / session
  start / `face_me`, rather than on every detection — directly bounds LLM cost (A).
- **Cache last-seen identity.** Keep the most recent identity per track so a brief
  occlusion or turn-away doesn't re-trigger a full recognition.
- **Confidence gate before LLM.** If a Mac/embedding path exists, use it as a cheap
  first pass and only escalate low-confidence cases to the LLM (a form of C).
- **Voice + face fusion.** Combine DoA / [[voice-match]] identity (`src/voice/doa.py`)
  with face identity to disambiguate when one signal is weak.
- **Enroll by conversation.** "This is Sam" captures the current face crop and
  labels it — registration without a separate UX.
- **Accumulate references over time.** Save a few crops per person across sessions
  (varied lighting/angle) to improve robustness, capped per person.

## Open questions

1. **LLM vs Mac service.** For ~10 people, is occasional LLM recognition (A)
   enough, or is a gallery/embedding service (B) needed?
2. **Enrollment / reference set.** How does a new person get registered — a few
   reference photos (A) or labeled embeddings (B)? Where does the capture UX
   live (the web dashboard already owns the camera URL)?
3. **Reference storage.** Reference photos (A) or labeled embeddings (B), and
   where they live (e.g. under `/home/pi/.config/robot-pet/`).
4. **Match confidence.** What counts as a confident match, and what's the
   uncertain band? (A returns this naturally; B needs a threshold.)
5. **Telemetry shape.** Extend the existing vision telemetry (face boxes) with an
   optional `name`/`confidence` per box, or publish a separate stream?

## Relationship to existing code

`robot-vision` stays the detector and the only thing reading camera snapshots for
perception. Recognition is added alongside it; the framework-agnostic detector
stays pure. A Mac service (B) would be a separate process and should not leak
into the Pi drivers.
