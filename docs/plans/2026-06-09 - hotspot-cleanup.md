# Hotspot Cleanup Plan

Goal: make the five reviewed hotspots easier to understand and safer to change
without turning the cleanup into a rewrite.

This plan is intentionally ordered from clearest fixes to riskiest refactors.
Each step should be useful on its own, covered by focused tests, and small
enough that a future agent can stop after any completed phase with the repo in a
better state.

Follow `AGENTS.md`: prefer direct code, no speculative abstractions, no service
layers, no managers, and no big-bang state-machine rewrite unless the smaller
steps prove inadequate.

## Baseline

Before changing behavior, run the currently relevant tests:

```bash
python3 -m unittest tests.test_safety_gate tests.test_motor tests.test_robot_motion tests.test_robot_voice tests.test_robot_voice_wake tests.test_voice_core
```

Current baseline from review: 136 tests passed.

Keep this command green after each phase. If a phase touches only one area, run
the focused test first, then the full command above before handing off.

## Phase 1 - Make RoboClaw Read Errors Match Write Errors

### Why

`src/drivers/motor.py` treats recoverable RoboClaw write failures as normal
hardware hiccups:

- `set_speed(...)` returns `False`
- `set_wheel_speeds(...)` returns `False`
- `stop()` logs and returns

The read methods currently let the same recoverable exception classes escape:

- `read_wheel_speeds()`
- `read_max_qpps()`
- `get_battery_voltage()`
- `get_currents()`

`robot_motion` catches telemetry read failures today, but the driver contract is
still inconsistent.

### Work

In `src/drivers/motor.py`:

1. Wrap each read method in `try/except Exception as exc`.
2. If `is_recoverable_roboclaw_error(exc)`, log a short warning and return the
   method's existing failure shape:
   - `read_wheel_speeds()` -> `(None, None)`
   - `read_max_qpps()` -> `(None, None)`
   - `get_battery_voltage()` -> `None`
   - `get_currents()` -> `None`
3. Re-raise non-recoverable exceptions.

Do not add a new helper on the first pass. Four small `try/except` blocks are
clearer than an abstraction here.

### Tests

In `tests/test_motor.py`:

- Add fake RoboClaw classes or methods that raise `PacketTimeoutError` from:
  - `ReadSpeedM1`
  - `ReadM1VelocityPID`
  - `ReadMainBatteryVoltage`
  - `ReadCurrents`
- Assert the read methods return the failure shapes above.
- Add one non-recoverable read exception test for one method to prove it still
  propagates.

### Acceptance

```bash
python3 -m unittest tests.test_motor
```

Then run the full baseline command.

## Phase 2 - Make the Safety Clamp Contract Obvious

### Why

`src/control/safety_gate.py::apply_safety_to_qpps()` does not mean "blocked
equals motors halt." It means:

> If safety is blocked and the requested command has forward motion, remove the
> forward component while preserving in-place rotation and reverse motion.

That behavior is defensible, and tests already encode it, but the current name
is too easy to misread at the call site.

### Work

In `src/control/safety_gate.py`:

1. Rename `apply_safety_to_qpps(...)` to a name that states the behavior, for
   example `cancel_forward_qpps_when_blocked(...)`.
2. Add a short docstring:

   ```python
   """Cancel unsafe forward motion while preserving rotation and reverse."""
   ```

3. Keep the current math and behavior for the first patch.
4. Update the import and call site in `src/robot_motion.py`.
5. Update tests in `tests/test_safety_gate.py`.

Do not split this into `is_blocked` plus a separate clamp unless a third caller
appears. There is one behavior and one call site today.

### Decision To Keep Explicit

The current behavior preserves rotation even when the block reason is
`sensors_stale`. That might be what we want, but it is a policy decision.

For this phase, preserve current behavior. If a future hardware test says stale
sensors should force a full stop, make that as a separate behavior change with a
specific test:

```text
safety.reason == "sensors_stale" -> return 0, 0
```

### Tests

In `tests/test_safety_gate.py`:

- Rename the existing test to make the contract clear, e.g.
  `test_blocked_safety_cancels_forward_but_preserves_rotation_and_reverse`.
- Keep the existing cases:
  - `(200, 200)` -> `(0, 0)`
  - `(200, 0)` -> `(100, -100)`
  - `(-200, 200)` -> `(-200, 200)`
  - `(-200, -200)` -> `(-200, -200)`

In `tests/test_robot_motion.py`, the existing safety tests should still pass.

### Acceptance

```bash
python3 -m unittest tests.test_safety_gate tests.test_robot_motion
```

Then run the full baseline command.

## Phase 3 - Remove Voice Playback RMS Drift

### Why

`PLAYBACK_RMS_STALE_SECS = 0.25` exists in both:

- `src/voice/assistant.py`
- `src/robot_voice.py`

`robot_voice.py` already imports `effective_playback_rms(...)` from
`voice.assistant`, but `_sample_timeline()` duplicates the stale-RMS check by
hand. This is a silent drift risk and an easy fix.

### Work

In `src/robot_voice.py`:

1. Delete the local `PLAYBACK_RMS_STALE_SECS`.
2. In `_sample_timeline()`, replace:

   ```python
   playback_rms = levels.playback_rms if now - levels.playback_at <= PLAYBACK_RMS_STALE_SECS else 0
   ```

   with:

   ```python
   playback_rms = effective_playback_rms(levels, now)
   ```

3. Keep the existing `refresh_barge_in_gate(...)` call for now.

Do not move barge-in gate code between modules in this phase. The goal is just
to remove the duplicate constant.

### Tests

Add or update a `TimelineBuffer` / `_sample_timeline` test in
`tests/test_robot_voice.py` if one exists nearby. The test should prove stale
playback RMS samples publish as `0`.

If `_sample_timeline()` is awkward to test directly, add the smallest local test
that exercises it with a fake session and one cancelled sampler tick. Do not add
a new test harness class unless existing tests already use one.

### Acceptance

```bash
python3 -m unittest tests.test_robot_voice tests.test_voice_core
```

Then run the full baseline command.

## Phase 4 - Reduce `robot_motion._run_motor_loop()` Without Changing Behavior

### Why

`src/robot_motion.py::_run_motor_loop()` interleaves command selection, voice
motion intents, safety, motor writes, idle release, and telemetry scheduling.
Telemetry reads are already extracted behind `_publish_telemetry()` and
`_read_next_telemetry_value()`, so this is not a total mess, but the loop still
has too much branching for the method that owns motor safety.

### Work

Keep this as a readability refactor. Do not change motor behavior.

Suggested small steps:

1. Extract command selection into one method that returns the data the loop
   already computes:

   ```python
   def _next_motion_target(self, now: float, drive: DriveCommand | None) -> tuple[DriveCommand | None, dict[str, Any] | None, int, int]:
   ```

   Only do this if the resulting signature stays readable. If the tuple becomes
   confusing, use a tiny dataclass named around the existing concept, e.g.
   `MotionTarget`. This is allowed because it replaces a safety-critical pile of
   parallel locals.

2. Keep safety application in `_run_motor_loop()` so the motor loop visibly owns
   the final clamp before commanding hardware:

   ```python
   safety = evaluate_safety(...)
   left_qpps, right_qpps = cancel_forward_qpps_when_blocked(...)
   ```

3. Extract the idle/zero-target handling only if it removes duplication. There
   are currently two similar blocks:
   - no drive and no intent
   - computed target is zero

   A small method like `_handle_zero_target(...)` is acceptable if it returns a
   plain action string such as `"continue"`, `"break"`, or `"done"`. If that
   feels contorted, leave the duplication and just add a short comment.

4. Keep `_read_next_telemetry_value()` separate. Do not put telemetry reads back
   into the main loop.

### Tests

Rely mostly on existing `tests/test_robot_motion.py` behavior tests:

- safety blocks forward QPPS
- stale sensors remove forward motion
- stale drive command stops motor
- motion intent runs without gamepad command
- telemetry reads rotate across publish ticks

Add one test only if the refactor exposes a specific edge case not already
covered.

### Acceptance

```bash
python3 -m unittest tests.test_robot_motion
```

Then run the full baseline command.

## Phase 5 - Make `robot_voice.py` Less Coupled Without Splitting the Service

### Why

`RobotVoiceService` is large, but a large service file is not automatically a
bug. The real issues are:

- state is represented by `_mode` strings (`None`, `"armed"`, `"active"`)
- wake/session lifecycle and dashboard timeline sampling live in the same class
- it imports barge-in helpers from `voice.assistant`

This phase should reduce coupling where it is cheap. Do not split the file into
a service layer hierarchy.

### Work

After Phase 3:

1. Replace `_mode` string comparisons with module constants:

   ```python
   VOICE_STOPPED = None
   VOICE_ARMED = "armed"
   VOICE_ACTIVE = "active"
   ```

   Or use a tiny `Literal` type alias if that reads better. Avoid an enum unless
   it clearly makes tests simpler.

2. Rename `_mode` only if it helps the call sites, e.g. `_voice_mode`.
   This is optional and should be one mechanical patch if done.

3. Keep `TimelineBuffer` in `robot_voice.py` for now. It has direct dashboard
   context and does not need a new module until another file wants it.

4. Do not move wake orchestration to a new class. If the method ordering is hard
   to follow, reorder methods into lifecycle groups instead:
   - command socket
   - orchestrator lifecycle
   - wake loop
   - session lifecycle
   - DoA
   - timeline/publish

### Tests

Existing tests should cover this:

```bash
python3 -m unittest tests.test_robot_voice tests.test_robot_voice_wake
```

If mode constants are introduced, add no tests just for constants. The existing
command and wake tests should catch broken mode transitions.

### Acceptance

The service still starts, arms, activates, deactivates, and responds to command
socket requests exactly as before.

Run the full baseline command.

## Phase 6 - Start Untangling `handle_scribe_events()` Safely

### Why

`src/voice/assistant.py::handle_scribe_events()` owns turn orchestration,
barge-in, debounce, history commits, phase telemetry, and cancellation. It has
many nested functions mutating `TurnRuntimeState`.

A full rewrite into an explicit state machine might eventually be right, but it
is too risky as the next move. The safer path is to extract the pieces that are
already conceptually independent and test them.

### Work, Part A - Group Barge-In Audio Memory

The most confusing state today is the parallel family:

- `recent_barge_in_*`
- `utterance_barge_in_*`
- current `gate_*`

Add small methods directly on `TurnRuntimeState` or small top-level functions.
Pick whichever keeps call sites simpler. Good candidates:

```python
def reset_utterance_barge_in_audio(state: TurnRuntimeState) -> None:
    ...

def note_utterance_barge_in_audio(state: TurnRuntimeState, now: float) -> None:
    ...

def reset_recent_barge_in_audio(state: TurnRuntimeState) -> None:
    ...

def note_recent_barge_in_audio(state: TurnRuntimeState, now: float, policy: TurnPolicy) -> None:
    ...
```

Do not create a separate class just for these fields on the first pass. Moving
the existing nested functions out of `handle_scribe_events()` is enough.

Update `handle_scribe_events()` to call the extracted functions. Behavior should
not change.

### Tests, Part A

Add focused tests near `DecideBargeInDuringPlaybackTest` in
`tests/test_voice_core.py`:

- recent barge-in audio resets all recent fields
- utterance barge-in audio resets all utterance fields
- note recent audio keeps the highest RMS inside the local speech window
- note recent audio resets gate memory when the window is stale
- utterance gate remains open if any observed gate was open

Run:

```bash
python3 -m unittest tests.test_voice_core
```

### Work, Part B - Extract Playback Barge-In Side-Effect Blocks

Partial and commit handling both do the same broad sequence during assistant
playback:

1. mark assistant speech started
2. publish current barge-in state
3. decide barge-in
4. report decision
5. either reject or cancel current turn
6. maybe start another turn

Keep decisions and side effects easy to see. A good first extraction is a helper
that only performs steps 1-4 and returns `BargeInOutcome`:

```python
def consider_playback_barge_in(source: str, text: str, now: float, active_turn: ActiveTurn) -> BargeInOutcome:
    ...
```

This may need to stay nested because it calls `publish_barge_in_state()` and
`report_barge_in()`. That is fine. The goal is to remove duplicated control
flow, not force every helper top-level.

Then leave the source-specific behavior in `handle_partial()` and
`handle_commit()`:

- partial accepted explicit interrupt -> cancel only, return to listening
- partial accepted non-explicit -> cancel, debounce stable partial
- commit accepted explicit interrupt -> cancel only, return to listening
- commit accepted non-explicit and commit accepted by policy -> cancel and
  start committed turn
- rejected -> emit rejection and preserve/return status

### Tests, Part B

Existing `handle_scribe_events()` tests should remain the main safety net.
Before touching this section, identify the test names that cover:

- explicit partial interrupt during playback
- explicit commit interrupt during playback
- low-RMS commit rejected during playback
- accepted non-explicit barge-in can start a new turn
- assistant echo is suppressed

If any of these are missing, add the missing test before the refactor.

Run:

```bash
python3 -m unittest tests.test_voice_core
```

### Work, Part C - Only Then Consider an Explicit State Name

After Parts A and B, reassess whether a state enum is still needed. If the code
is readable enough, stop.

If state is still implicit and bug-prone, add a tiny state name around the user
visible phases:

```python
TURN_LISTENING = "listening"
TURN_HEARING = "hearing"
TURN_THINKING = "thinking"
TURN_SPEAKING = "speaking"
```

Do not build a generic transition table. The repo does not need that. The
benefit would be to make status transitions searchable and typo-resistant.

### Acceptance

For Phase 6, acceptance is not "we have a perfect state machine." Acceptance is:

- `TurnRuntimeState` owns less hand-synchronized mystery.
- The recent/utterance barge-in transitions can be unit-tested without running
  the full async event loop.
- Partial and commit playback barge-in paths are visibly parallel.
- `tests.test_voice_core` passes.
- The full baseline command passes.

## Phase 7 - Reassess Remaining Voice Architecture

After the lower-risk cleanup, revisit the deeper questions from
`docs/plans/2026-05-24 - voice-architecture-notes.md`:

- Is hardware AEC working well enough to reduce text-similarity echo suppression?
- Is speculative turn execution worth its complexity?
- Can the local upload gate and barge-in gate share more state?

Do not answer those questions by refactoring. Measure first, then either delete
unneeded behavior or stabilize the behavior we deliberately keep.

## Final Handoff Checklist

At the end of each completed phase:

- Update imports and names consistently.
- Keep comments short and behavior-oriented.
- Run the phase-specific tests.
- Run the full baseline command.
- Note any behavior intentionally preserved even if it still feels imperfect.

At the end of the whole plan:

```bash
python3 -m unittest tests.test_safety_gate tests.test_motor tests.test_robot_motion tests.test_robot_voice tests.test_robot_voice_wake tests.test_voice_core
```

Expected result: all tests pass, with no real hardware, network, or API keys.

## Completion Notes - 2026-06-09

The code cleanup phases in this plan are complete.

Completed implementation work:

- Phase 1: RoboClaw read methods now handle recoverable serial/timeouts like
  write methods and return their existing failure shapes.
- Phase 2: the safety clamp is named for its real contract:
  `cancel_forward_qpps_when_blocked(...)`, preserving rotation/reverse while
  cancelling unsafe forward motion.
- Phase 3: `robot_voice.py` no longer duplicates the playback RMS stale
  threshold; timeline sampling uses `effective_playback_rms(...)`.
- Phase 4: `_run_motor_loop()` keeps final safety and motor-command decisions
  in the loop, but target shaping for intent/gamepad commands is split into
  small local helpers.
- Phase 5: `robot_voice.py` uses simple mode constants instead of raw
  `"armed"` / `"active"` string comparisons.
- Phase 6A: recent and utterance barge-in audio memory transitions are
  top-level helpers with focused tests.
- Phase 6B: partial and commit playback barge-in paths share the
  `consider_playback_barge_in(...)` side-effect block, while keeping their
  source-specific behavior local.

Phase 6C decision:

Do not add explicit turn/status constants right now. After Parts A and B, the
remaining `listening` / `hearing` / `thinking` / `speaking` strings are mostly
telemetry payload values at the status boundary. Replacing them with constants
would be a mechanical naming pass, not a meaningful state-machine improvement.
If future bugs show typo-prone status transitions, revisit this with a specific
failing test.

Phase 7 outcome:

The remaining voice-architecture questions cannot be answered by code cleanup in
this repo alone. They need hardware/runtime measurement:

- Test whether XVF3800 hardware AEC on the selected capture channel suppresses
  assistant playback enough to retire or shrink text-similarity echo
  suppression.
- Measure speculative-turn latency savings from partial transcripts versus
  committed transcripts. If savings are small, deleting speculation may be
  better than further refactoring it.
- Measure whether the local Scribe upload gate and barge-in gate can share a
  common speech signal without hurting barge-in reliability.

Do not refactor these areas further until those measurements are available.
