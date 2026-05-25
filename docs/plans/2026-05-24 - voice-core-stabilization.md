# Voice Core Stabilization Plan

Goal: make the voice stack easier to understand and safer to change without turning this into a rewrite.

The voice assistant is now kind of working. That matters. This plan is intentionally conservative: each phase should preserve the current working behavior, except where the behavior is clearly wrong or confusing. The main product invariant is:

> When the user says an explicit interrupt like "wait" while the assistant is speaking, robot speech stops immediately, the current turn is cancelled, and the system returns to listening without answering "wait."

Do not chase architecture purity. Prefer changes that make bugs like "barge-in detected but playback keeps going" obvious in code and testable without the robot.

## Current Pain

The hard bugs have not been caused by one bad threshold. They have come from hidden ownership and mixed meanings:

- `handle_scribe_events()` owns turn orchestration, barge-in decisions, history commits, phase telemetry, debounce timing, and cancellation side effects.
- `audio_levels` is an untyped shared dictionary used as both runtime state and dashboard telemetry.
- Playback state is split across `playback_event`, `speaking_event`, ReSpeaker queue state, dashboard phase state, and task cancellation.
- "Cancel turn" and "stop speaker output" are different operations, but they have often been treated as if one implies the other.
- Scribe partials, commits, local RMS, playback RMS, and assistant echo suppression are all interleaved in one event loop, which makes small fixes easy to aim at the wrong subsystem.

## Worth-It Filter

A refactor is worth doing if it satisfies at least one of these:

- It would have made the recent barge-in failure easier to diagnose.
- It reduces the chance that input processing can be blocked by playback cleanup.
- It creates a small testable unit for a user-visible voice behavior.
- It names a currently implicit state or lifecycle boundary.
- It removes a confusing behavior path, especially around explicit interrupts.

A refactor is probably not worth doing right now if it only:

- Moves code between files without clarifying ownership.
- Adds generic frameworks, managers, factories, or plugin points.
- Replaces explicit config parsing with clever reflection.
- Tries to solve echo cancellation or audio routing as a big new abstraction.
- Requires multiple simultaneous behavior changes to validate.

## Phase 0 - Pin Current Behavior Before More Refactors

### Goal

Lock down the behaviors we care about so refactors do not quietly regress voice.

### Work

Add focused tests around the current event loop and playback contract:

- Explicit partial interrupt during playback:
  - input: active assistant turn, local RMS high, partial text `"wait"`
  - expected: playback stop hook fires, turn is cancelled, no new speculative turn starts.
- Explicit committed interrupt during playback:
  - input: active assistant turn, local RMS high, commit text `"stop"`
  - expected: playback stop hook fires, turn is cancelled, no new turn starts for interrupt-only commits.
- Non-explicit follow-up during playback:
  - input: active assistant turn, local RMS/gate open, partial or commit like `"actually tell me about motors"`
  - expected: old playback stops and a new turn can start when the policy accepts it.
- Echo suppression:
  - input: assistant is speaking, Scribe hears text similar to current assistant output
  - expected: no barge-in, no new turn.
- Stop path cannot block input:
  - input: playback stop hook hangs forever
  - expected: later `audio_activity` events still update status.

Some of these already exist. Keep them together and name them around the user scenario, not the implementation detail.

### Acceptance

- `uv run python -m unittest tests.test_voice_core` passes.
- The tests can be read as a checklist for manual robot validation.
- No network or real audio hardware is needed.

## Phase 1 - Make Explicit Interrupts a First-Class Behavior

### Goal

Remove ambiguity around words like "wait" and "stop."

### Behavior

When accepted during assistant playback:

- `wait`, `stop`, `pause`, `cancel`, and `no` mean stop the current assistant speech.
- They do not start a speculative turn.
- They do not produce an assistant reply like "I'm here."
- They return the session to listening.
- Cleanup of OpenAI / ElevenLabs tasks happens after the speaker has been told to stop.

This is a behavior change only where the old behavior was confusing.

### Work

Keep the policy decision in `TurnPolicy`, but make the orchestration explicit:

```text
barge decision returns explicit_interrupt
  -> abort playback now
  -> cancel active turn
  -> clear debounce
  -> listening status
  -> do not start_turn(text)
```

For non-explicit accepted barge-in:

```text
barge decision returns substantial_partial
  -> abort playback now
  -> cancel active turn
  -> start a new turn after the existing stable-partial delay
```

### Acceptance

- Unit tests cover partial and commit interrupts.
- Dashboard timeline shows `BARGE` / `cancel`, then playback RMS drops promptly.
- Repeating `"wait wait wait"` does not generate an assistant response.

## Phase 2 - Define the Playback Contract

### Goal

Make it impossible to confuse "turn cancelled" with "speaker stopped."

### Target Contract

Keep this small and local. Do not add a generic audio bus.

Playback operations should have plain meanings:

- `begin_playback()` starts a new output queue and returns an id.
- `write_output(pcm)` queues PCM for the current playback.
- `end_playback(playback_id, drain=True)` gracefully finishes that playback if it is still current.
- `stop_playback_now()` immediately clears queued output and the hardware output buffer. It must not await websocket cleanup or input processing.

Voice turn operations should call playback operations deliberately:

- Normal TTS completion uses `end_playback(..., drain=True)`.
- Cancelled TTS cleanup uses `end_playback(..., drain=False)` as a fallback.
- Barge-in uses `stop_playback_now()` immediately, before task cleanup.

### Work

- Document the contract in `src/drivers/respeaker.py` near the playback methods.
- Keep `VoiceSession.stop_playback_now()` as the only callback passed into orchestration.
- Make `handle_scribe_events()` fire this callback without awaiting it.
- Ensure `_speak()` final cleanup remains id-safe so an old turn cannot drain or stop a new playback session.

### Acceptance

- Tests prove a stuck stop callback cannot block scribe event handling.
- Tests prove `end_playback(old_playback_id)` cannot affect a newer playback.
- Manual robot test: story -> "wait" -> audible silence within about 100-200 ms.

## Phase 3 - Introduce Typed Runtime State

### Goal

Replace the easiest-to-break shared dictionaries and loose locals with named state, without changing behavior.

### Work

Add a small dataclass for audio/dashboard state, for example:

```python
@dataclass
class AudioLevels:
    mic_rms: int = 0
    mic_peak: int = 0
    mic_last: int = 0
    playback_rms: int = 0
    playback_at: float = 0.0
    threshold_rms: int = 0
    gate_open: bool = False
    scribe_gate_open: bool = False
    gate_above_since: float | None = None
```

Keep conversion to dict at the telemetry boundary if the dashboard expects dictionaries.

Then add a small turn orchestration state object for the locals inside `handle_scribe_events()`:

```python
@dataclass
class TurnRuntimeState:
    active_turn: ActiveTurn | None = None
    next_turn_id: int = 0
    debounce_task: asyncio.Task[None] | None = None
    last_local_speech_at: float = 0.0
    last_local_speech_rms: int = 0
    recent_barge_in_mic_rms: int = 0
    recent_barge_in_audio_at: float = 0.0
```

Do not overdo this. If moving a local into the dataclass makes the code harder to read, leave it local.

### Acceptance

- No behavior changes.
- Tests still pass.
- A reader can find the complete set of barge-in runtime state in one place.
- Dashboard telemetry still has the same field names.

## Phase 4 - Split Decisions From Side Effects In `handle_scribe_events`

### Goal

Make the orchestration readable enough that a future bug report maps to one small block of code.

### Work

Extract small pure-ish decision helpers, not classes:

- `decide_partial_during_playback(...)`
- `decide_commit_during_playback(...)`
- `should_suppress_assistant_echo(...)` if the current nested function remains noisy
- `record_audio_activity(...)` if the RMS/gate update remains hard to read after Phase 3

These helpers should return simple results like:

```python
("ignore", reason)
("cancel_only", reason)
("cancel_and_start", reason)
("start", reason)
```

Or use a tiny dataclass if that is clearer. Avoid an enum unless strings are getting typo-prone in tests.

The side-effect code should remain in `handle_scribe_events()`:

- emit timeline events
- call `stop_playback_now`
- cancel tasks
- start turns
- update history

### Acceptance

- Partial and commit paths no longer duplicate the same barge-in decision block.
- Tests for decision helpers do not need tasks, queues, OpenAI fakes, or audio fakes.
- Existing scenario tests still pass.

## Phase 5 - Clean Up Config Into Groups

### Goal

Make tuning understandable without adding a sprawling config system.

### Work

Keep `VoiceConfig` explicit, but group fields by comments and names:

- device/audio:
  - input/output device
  - sample rate/channels
  - input/output gain
- assistant voice:
  - voice ids
- barge-in:
  - enabled
  - min RMS
  - sustain
  - cooldown
  - explicit interrupts
  - echo similarity
- wake/session:
  - wake model/chime/idle fields

Then decide whether `TurnPolicy` or `VoiceConfig` owns defaults. Prefer:

- `VoiceConfig` owns user-facing defaults.
- `turn_policy_from_config()` maps those into `TurnPolicy`.
- `TurnPolicy` can keep defaults for tests, but tests that care about deployed behavior should construct it from `VoiceConfig`.

Do not add new config knobs unless a real tuning problem demands them.

### Acceptance

- Config tests still pass.
- Barge-in defaults are visible in one user-facing place.
- No dashboard behavior changes required in this phase.

## Phase 6 - Split ElevenLabs STT And TTS Files

### Goal

Reduce file-level confusion where Scribe input and Flash output are mixed together.

### Work

Move without changing behavior:

- `stream_audio_to_scribe`, Scribe constants, and Scribe gate helpers to `src/voice/scribe_io.py`.
- `speak_with_eleven_flash`, Flash constants, websocket close timeout, and TTS helpers to `src/voice/tts_io.py`.
- Keep `pcm16_rms` in `turn_policy.py` only if policy/tests use it, or move it to a tiny `voice/audio_math.py` if both Scribe and TTS need it.

This is worth doing only after Phases 1-5, because file splitting alone would not have prevented the recent bug.

### Acceptance

- Imports are updated.
- Tests pass.
- No behavior changes.
- The old `elevenlabs_io.py` is deleted or reduced to a compatibility wrapper only if necessary. Prefer deleting it.

## Phase 7 - Improve Scenario Testing

### Goal

Make future changes safer without requiring the robot for every edit.

### Work

Add a tiny test helper for voice scenarios:

```python
scenario = VoiceScenario(policy=...)
await scenario.user_commit("Tell me a story")
await scenario.assistant_playing()
await scenario.audio_activity(rms=900)
await scenario.user_partial("wait")
scenario.assert_playback_stopped()
scenario.assert_no_new_turn()
```

Keep it in tests. Do not build a production simulation framework.

### Acceptance

- Existing verbose tests around `handle_scribe_events()` get shorter.
- New tests read like product behavior.
- The helper does not hide important timing; it should still make sleeps/clock advances visible.

## Deferred / Not Worth It Yet

These may be good someday, but not now:

- A generic voice state machine framework.
- A service layer or manager class around every subsystem.
- A plugin/event bus for voice events.
- A dashboard rewrite for voice config.
- Software echo cancellation in Python.
- A full fake clock across the whole async stack.
- Splitting every helper in `assistant.py` into separate files.
- Replacing explicit config parsing with reflection.

## Suggested PR Order

Keep each PR small enough that it can be manually tested on the robot.

1. **Behavior pinning and explicit interrupt tests**
   - Mostly tests.
   - Any tiny behavior cleanup for explicit interrupts.

2. **Playback contract cleanup**
   - `stop_playback_now()` semantics and docs.
   - Id-safe cleanup tests.
   - Manual barge-in validation.

3. **Typed audio state**
   - Replace `audio_levels` dict internally.
   - Preserve telemetry field names.

4. **Turn runtime state + decision helpers**
   - Reduce `handle_scribe_events()` complexity.
   - Keep all current scenario tests green.

5. **Config grouping**
   - No new behavior.
   - Clarify defaults and mapping to `TurnPolicy`.

6. **Scribe/TTS file split**
   - Mechanical once behavior is covered.

7. **Scenario test helper**
   - Optional, but useful before larger future voice changes.

## Manual Regression Checklist

Run this after PRs 2, 4, and 6 at minimum:

- [ ] Start `robot-voice`; dashboard mic RMS moves when speaking.
- [ ] Say "Tell me a story"; assistant starts speaking.
- [ ] Say "wait" once; speech stops quickly and no "I'm here" response follows.
- [ ] Say "wait wait wait" repeatedly; mic/activity lanes continue moving and speech stays stopped.
- [ ] Ask a normal follow-up while assistant is speaking; old speech stops and a new turn starts.
- [ ] Let assistant finish a short answer; history still commits.
- [ ] Assistant saying words like "stop" in its own response does not self-cancel.
- [ ] Toggle voice off/on; mic and playback still work.
- [ ] Dashboard timeline shows mic, scribe, hearing/thinking/speaking, barge events, and playback RMS.

## Success Criteria

This plan is successful when:

- The common voice loop still works at least as well as it does now.
- Explicit interrupts are boring and reliable.
- `handle_scribe_events()` is no longer the only place someone can understand the system.
- Playback cancellation has one obvious owner and one obvious fast path.
- New voice behavior bugs can usually be reproduced in a unit/scenario test before touching the robot.
