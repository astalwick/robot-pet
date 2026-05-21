# Dynamic Barge-In Gating Plan

Goal: stop the assistant from interrupting itself when the ReSpeaker hears TTS playback, while keeping natural user interruption possible.

The current behavior does not work well enough: mic audio keeps streaming to Scribe during TTS, Scribe can transcribe the assistant's own voice, and the current local gate only checks recent mic RMS. That is too easy for speaker bleed to satisfy.

This plan keeps the system simple. Do not add a generic audio routing layer, a plugin policy system, or a separate "barge-in manager." Keep the logic near the existing voice modules unless the code repeats three times.

## Current State

- `src/drivers/respeaker.py` captures 16 kHz, 6-channel ReSpeaker audio and extracts one mono channel, currently defaulting to channel index `1`.
- `src/voice/elevenlabs_io.py` streams every mic chunk to ElevenLabs Scribe with static VAD settings.
- `src/voice/assistant.py` tracks when TTS is speaking via `speaking_event`.
- `src/voice/turn_policy.py` decides whether a Scribe partial can barge in while the assistant is speaking.
- `src/config/voice.py` persists voice config to `/home/pi/.config/robot-pet/voice.json`.
- `src/robot_web_dashboard.py` exposes that config through `/config/voice`.
- The dashboard already exposes `input_gain` and `output_gain` as quick tuning sliders.

Useful existing behavior:

- Bare interrupt words can cancel the assistant during TTS: `stop`, `wait`, `no`, `cancel`, `pause`.
- Assistant echo suppression compares Scribe text against recently streamed assistant text.
- Commits and partials that look like recent assistant echo are ignored.

Main gap:

- The "local speech" signal is just mic RMS crossing one fixed threshold recently. It does not know whether the mic energy is likely user speech or the assistant's own playback.

## Direction

Use a dynamic local audio gate during TTS.

The assistant should still listen while speaking, but it should treat TTS-time STT as untrusted unless local audio evidence says a user is actually speaking over the robot.

Decision shape during TTS:

```text
Scribe partial arrives
  -> if it looks like assistant echo, ignore
  -> if local near-end gate is closed, ignore
  -> if explicit interrupt is strong enough, cancel quickly
  -> if sustained speech is strong enough, cancel and start a new turn
```

The local near-end gate should be based on:

- current mic RMS
- recent mic RMS duration
- current playback RMS
- a configurable playback leakage ratio
- a configurable minimum interrupt floor

The gate should not need perfect echo cancellation. It only needs to avoid treating normal speaker bleed as a user interruption.

## Proposed Tuning Levers

Add these fields to `VoiceConfig` and expose them in `/config/voice` / dashboard config.

Recommended first-pass fields:

- `barge_in_enabled: bool = True`
  - Master switch for interrupting during TTS.
  - If false, ignore barge-in while `assistant_speaking=True`.

- `barge_in_min_words: int = 3`
  - Minimum words for non-explicit barge-in.
  - Current equivalent is `min_barge_in_words`.

- `barge_in_min_chars: int = 12`
  - Minimum chars for non-explicit barge-in.
  - Current equivalent is `min_barge_in_chars`.

- `barge_in_cooldown_secs: float = 0.35`
  - Minimum time after TTS starts before normal barge-in can happen.
  - Explicit interrupt can still bypass this if configured to do so.

- `barge_in_min_rms: int = 700`
  - Absolute mic RMS floor during TTS.
  - This should start higher than the current `LOCAL_SPEECH_RMS_THRESHOLD = 500`.

- `barge_in_sustain_ms: int = 350`
  - Required continuous near-end audio before accepting normal barge-in.
  - This is the main fix for one-off speaker bleed and transients.

- `barge_in_playback_leakage_ratio: float = 1.8`
  - Dynamic threshold multiplier against playback RMS.
  - During TTS, require mic RMS to exceed `playback_rms * ratio`, also respecting `barge_in_min_rms`.
  - This name is intentionally plain, even if it is not mathematically perfect.

- `barge_in_explicit_interrupts: str = "stop,wait,no,cancel,pause"`
  - Comma-separated words the dashboard can tune without adding list editing UI.
  - Keep the config parser simple: split on commas, trim whitespace, lowercase.

- `barge_in_explicit_requires_sustain: bool = False`
  - If false, an explicit interrupt can cut in quickly once above the dynamic RMS floor.
  - If true, explicit interrupts also need `barge_in_sustain_ms`.

- `assistant_echo_similarity: float = 0.9`
  - Dashboard-tunable version of the existing echo text match threshold.
  - Useful when the assistant's speech is paraphrased or partially misrecognized.

Optional later fields, only if the first pass is not enough:

- `barge_in_wake_words: str = ""`
  - Empty means no wake word required.
  - Example: `bloop,hey bloop`.
  - If set, require one of these during TTS before accepting non-explicit barge-in.

- `tts_mic_mute_mode: str = "dynamic"`
  - Values: `dynamic`, `ignore_during_tts`.
  - Only add this if we want a blunt fallback for demos.

Avoid adding a long list of audio constants to config before they are needed. These levers are enough to tune behavior in a real room without turning the dashboard into a DSP lab.

## Implementation Stages

### Stage 1 - Make TurnPolicy Configurable

Wire the relevant `VoiceConfig` fields into `TurnPolicy` when `VoiceSession` starts.

Keep the defaults in one place:

- Either keep `TurnPolicy` defaults as the canonical runtime defaults and have `VoiceConfig` mirror them.
- Or make `VoiceConfig` the canonical user-facing defaults and construct `TurnPolicy` from config.

Prefer the second option because the dashboard config is what the robot operator sees.

Acceptance:

- Existing tests pass with default config.
- A config value like `barge_in_enabled=False` changes policy behavior without editing constants.
- `/config/voice` returns the new fields.

### Stage 2 - Track Playback RMS

Compute a simple RMS for each TTS PCM chunk before writing it to the ReSpeaker.

The least invasive path:

- `speak_with_eleven_flash` already receives decoded PCM chunks.
- Before `audio_writer(audio)`, compute `pcm16_rms(audio)`.
- Publish the current playback RMS to the voice session through a tiny callback or shared local state.
- Clear or decay it when TTS stops.

Do not add a new audio abstraction for this. It is just one measurement on bytes we already have.

Acceptance:

- During TTS, `handle_scribe_events` can see a recent playback RMS value.
- When TTS ends, playback RMS no longer keeps the dynamic gate artificially high.
- Tests can fake playback RMS without real audio hardware.

### Stage 3 - Add Sustained Near-End Gate

Track recent mic activity during TTS as a short rolling state:

- mic RMS
- timestamp
- whether it exceeded the current dynamic threshold
- how long it has continuously exceeded the threshold

During TTS:

```text
dynamic_threshold = max(
  barge_in_min_rms,
  playback_rms * barge_in_playback_leakage_ratio
)
```

Then:

- If mic RMS is below `dynamic_threshold`, gate is closed.
- If mic RMS stays above `dynamic_threshold` for `barge_in_sustain_ms`, gate opens.
- Explicit interrupts may bypass sustain if `barge_in_explicit_requires_sustain=False`, but should still require the dynamic RMS threshold.

Keep the state small. This can live inside `handle_scribe_events` next to `last_local_speech_at` and `last_local_speech_rms`.

Acceptance:

- A single loud playback-like RMS event does not allow normal barge-in.
- Sustained high mic RMS during TTS allows normal barge-in.
- Explicit interrupt words still work when loud enough.
- Assistant echo text still blocks self-interruption.

### Stage 4 - Gate Scribe Events, Not Capture

First pass should keep sending mic audio to Scribe and gate the resulting events in `handle_scribe_events`.

Reason: this preserves the current websocket lifecycle and keeps the change small. We can still use Scribe partials for text, but only trust them after the local gate opens.

Do not stop and restart the Scribe websocket during every TTS response in the first pass. That adds timing problems and may make real barge-in feel worse.

Acceptance:

- The assistant can still be interrupted mid-TTS.
- Self-transcribed assistant speech is ignored.
- Scribe websocket behavior remains stable.

### Stage 5 - Dashboard Config

Expose the new fields in `VOICE_FIELDS`.

Dashboard UI direction:

- Keep the existing compact voice card for everyday controls.
- Add the most useful barge-in controls to the voice card:
  - `barge_in_enabled`
  - `barge_in_min_rms`
  - `barge_in_sustain_ms`
  - `barge_in_playback_leakage_ratio`
- The less common fields can appear in the existing config modal/form if that is already generated from `/config/voice`.

If the current dashboard config form does not render arbitrary voice fields cleanly, make the smallest local improvement needed. Do not build a generic form framework.

Recommended slider ranges:

- `barge_in_min_rms`: `100 .. 5000`, step `50`
- `barge_in_sustain_ms`: `0 .. 1500`, step `50`
- `barge_in_playback_leakage_ratio`: `0.5 .. 5.0`, step `0.1`
- `assistant_echo_similarity`: `0.5 .. 1.0`, step `0.05`
- `barge_in_cooldown_secs`: `0.0 .. 2.0`, step `0.05`

Acceptance:

- Changing a barge-in slider writes `voice.json`.
- `robot-voice` picks up the changed config through its existing config polling and restarts the session.
- The dashboard shows the active values from telemetry or config.

### Stage 6 - Telemetry For Tuning

Add just enough telemetry to tune this without SSHing into logs.

Recommended telemetry fields:

- `barge_in_enabled`
- `barge_in_threshold_rms`
- `barge_in_mic_rms`
- `barge_in_playback_rms`
- `barge_in_gate_open`
- `barge_in_last_reason`

Keep these as voice telemetry fields, not a new stream.

Useful reasons:

- `disabled`
- `assistant_not_speaking`
- `low_rms`
- `not_sustained`
- `assistant_echo`
- `cooldown`
- `explicit_interrupt`
- `substantial_partial`

Acceptance:

- The dashboard can show why interruption did or did not happen.
- Logs and tests use the same reason strings where practical.

### Stage 7 - Hardware Validation

Test on the robot in a real room.

Scenarios:

- Assistant speaks a normal short answer. It should not interrupt itself.
- Assistant says one of the explicit words, like "stop", in its own sentence. It should not cancel itself unless the user also speaks.
- User says "stop" while assistant is speaking. It should stop quickly.
- User asks a full follow-up while assistant is speaking. It should stop and start a new turn.
- User speaks quietly near the robot. Tune whether this should work.
- Speaker volume is changed. Dynamic threshold should adapt.
- `output_gain` is raised. Self-barge-in should not come back immediately.

Record the chosen tuning values in this plan or a follow-up decision note.

## Cleanup / Refactor Notes

These are not required before the dynamic gate, but they would make this code easier to reason about.

- `TurnPolicy` currently mixes speculation, commit filtering, explicit interrupt handling, echo matching, and local audio gate constants. Keep it as one file for now, but group the methods by turn phase and rename the audio-specific values with a `barge_in_` prefix as fields move into config.
- `should_accept_barge_in` is now mostly a compatibility wrapper around `barge_in_decision`. If no callers need the wrapper after the config work, remove it.
- `handle_scribe_events` is the busiest function in the voice path. Do not split it into classes, but small top-level helpers for "update near-end gate" and "format gate reason" may be worth it once the sustained gate is added.
- `stream_audio_to_scribe` currently computes mic RMS and sends audio to Scribe in the same loop. That is fine, but once playback-aware gating exists, consider emitting a richer `audio_activity` event instead of only `rms`.
- Dashboard voice controls are currently special-cased for gain sliders. If barge-in gets several sliders, a tiny `voiceSliderRow(label, key, min, max, step)` helper would reduce repetition without becoming a generic form builder.
- `VoiceConfig.from_dict` will grow with the new fields. Keep it explicit. Do not replace it with reflection or dataclass magic; the explicit parsing is easier to audit when bad config can break the robot's voice service.

## Non-Goals

- Do not add software echo cancellation in Python in this pass.
- Do not change ReSpeaker channel selection unless validation shows channel `1` is wrong for this body.
- Do not require wake word by default.
- Do not stop mic capture during TTS by default.
- Do not add a new voice service process.
- Do not add ROS2 concepts to this path yet.
