# Voice Architecture Notes

Companion to `2026-05-24 - voice-core-stabilization.md`. The stabilization plan
optimizes for understanding and cleaning up what exists. This note steps back
and asks which architectural choices are *load-bearing* — i.e. which ones we
should question before refactoring the code that implements them.

The point is not to recommend a rewrite. It is to make the unusual choices
explicit, so future cleanup work is not just polishing decisions we never
deliberately made.

## What is unusual compared to typical voice assistant stacks

Most voice assistants today are built on one of:

- A single-vendor realtime API (OpenAI Realtime, Gemini Live).
- A pipeline framework (LiveKit Agents, Pipecat, Vocode) that owns turn-taking,
  VAD, and interruption.
- An open-source pipeline (Home Assistant Voice + Wyoming, Rhasspy).

We built a three-vendor stack by hand:

- ElevenLabs Scribe for STT.
- OpenAI Responses for LLM.
- ElevenLabs Flash for TTS.

This is defensible and gives us things the bundled stacks do not (voice cloning,
LLM choice). But it means turn-taking, barge-in, and echo handling are
*our* responsibility, because no vendor sees the whole conversation. That is
where most of the complexity in `handle_scribe_events` comes from.

## Specific oddities

### 1. Text-similarity echo suppression

`turn_policy.py:115` (`matches_assistant_echo`) does fuzzy string matching
between Scribe transcripts and what the assistant just said, and uses the
result to suppress barge-in. This is not a normal pattern. Standard solutions
are:

- Hardware AEC on the mic (the XVF3800 has on-chip AEC).
- WebRTC AEC / speexdsp in software, with a speaker reference signal.

We are reading channel 1 of 6 from the XVF3800 (`respeaker.py:115`,
`capture_channel_index=1`). On most XVF3800 firmwares channel 0 or 1 is
already the AEC-processed mic. If hardware AEC is working, the text-similarity
defense is mostly redundant. If it is not working, fixing it would remove a
class of bugs entirely.

**Question to answer:** is the XVF3800 hardware AEC actually doing useful work
on our chosen channel? If yes, can we delete `matches_assistant_echo` and the
echo-memory window?

### 2. Speculative turn execution

We start an OpenAI request from a *partial* Scribe transcript and then
cancel/replace it if the commit does not match. A large share of the locals
and helpers in `handle_scribe_events` exist only to support this:
`speculative`, `delay_playback`, `playback_release_task`,
`release_speculative_playback`, `release_committed_playback`,
`should_replace_speculative_prompt`, plus the partial/commit divergence.

The latency this saves is real (the LLM gets a head start during Scribe's
commit silence window), but it is also why so much state has to coordinate.
Most assistants accept the commit latency to keep orchestration simple.

**Question to answer:** how much wall-clock latency does speculation actually
save in our deployment? If it is 200–300 ms, the complexity is probably not
worth it. If it is 800+ ms because Scribe's `vad_silence_threshold_secs=1.0`
forces a long commit wait, then tuning the VAD threshold might recover most
of the gain at lower cost.

### 3. Three layers of VAD

We currently have three independent decisions about whether the user is
speaking:

- Server-side VAD at ElevenLabs (`vad_silence_threshold_secs`,
  `vad_threshold` in the Scribe URL params).
- Local upload gate (`elevenlabs_io.py:29`, `update_scribe_upload_gate`)
  using `USER_ACTIVE_RMS_THRESHOLD = 100`.
- Local barge-in gate (`assistant.py:90`, `update_near_end_gate`)
  using `barge_in_min_rms = 700` with sustain.

They do not share thresholds or timing. Most assistants run one VAD and use
its output everywhere. The upload gate sends silence (`b"\x00"`) rather than
withholding frames, which is unusual — it keeps the socket warm at the cost
of feeding the server VAD frames it has to evaluate.

**Question to answer:** can we reduce to one VAD (probably local Silero or
the existing RMS gate), and pass its output to all three consumers?

### 4. No explicit conversation state machine

We emit status strings (`listening`, `hearing`, `thinking`, `speaking`) for
the dashboard, but there is no `enum State` in the code. The current state
is inferred from combinations of
`(active_turn is not None, active_turn.is_playing_back(), active_turn.is_speaking())`.
This is why bugs of the form "we acted as if we were in state X but were
actually in state Y" are easy to introduce.

The stabilization plan's Phase 4 (decision helpers returning string tuples)
is a partial state machine in disguise. Making it explicit — a 20-line enum
with named transitions — is normal practice, not enterprise abstraction.

### 5. Telemetry dict doubling as control state

`audio_levels` is mutated by three files (`assistant.py`, `session.py`,
`elevenlabs_io.py`) and read by both the live decision path and the dashboard.
`gate_above_since` drives barge-in sustain timing *and* appears on the
dashboard. `scribe_gate_open` is written by `elevenlabs_io.py` and read by
`assistant.py` through string keys on the same dict.

The stabilization plan's `AudioLevels` dataclass would fix the typing. It
would not fix the cross-file mutation pattern. The deeper question is
whether dashboard telemetry should be derived at the edge from typed control
state, instead of sharing a mutable dict.

## What the stabilization plan does and does not cover

The plan tackles points 4 (partially, via decision helpers) and 5 (typing
fix only). It does not address points 1, 2, or 3. Those are scope-reduction
questions, not refactoring questions — and the answer to "should we keep
speculative turns?" determines whether ~30 % of the orchestration code
should be cleaned up or deleted.

A more complete sequence would be:

1. Measure: is hardware AEC working? What does speculation actually save?
2. Decide which of the unusual choices to keep based on the measurements.
3. Then run the stabilization plan against the simpler system.

If we skip steps 1–2 and go straight to the stabilization plan, we may end
up cleanly refactoring code that did not need to exist.

## What we get from the hand-stitched stack that we cannot get elsewhere

This is the load-bearing question. The current stack gives us:

- ElevenLabs voice cloning (personality).
- A real LLM (gpt-5-class), not a constrained realtime model.
- ~1 s turn-taking latency.

Any move toward a typical stack has to preserve those, or it is not worth
making. See the conversation thread for the analysis of which framework
options can hold all three.
