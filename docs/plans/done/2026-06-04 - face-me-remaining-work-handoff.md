# `face_me` Remaining Work Handoff

## Objective

Finish the first production version of `face_me`:

1. `robot-voice` continuously samples ReSpeaker direction of arrival (DoA).
2. It caches the most recent stable direction from active speech.
3. The assistant can call a parameterless `face_me` tool.
4. `robot-voice` sends the cached signed relative angle to `robot-motion`.
5. `robot-motion` performs one slow, bounded, gamepad-preemptible timed turn.

Do not continuously steer toward live DoA while the robot is moving. This
version should make one turn using the direction captured while the user spoke.

The measured calibration and raw USB behavior are documented in:

```text
docs/plans/2026-06-04 - respeaker-doa-face-me.md
```

## Current State

Already implemented:

- `src/drivers/respeaker.py`
  - `ReSpeakerDoA.open()` opens USB device `2886:001e`.
  - `ReSpeakerDoA.read()` returns `DoAReading(angle_degrees, speech_detected)`.
  - The verified firmware payload interpretation is implemented.
- `setup.sh`
  - Installs a udev rule granting the `audio` group access to the ReSpeaker USB
    control device.
- `src/control/motion_intent.py`
  - Has a diagnostic-only timed turn using explicit
    `toward_left_wheel` / `toward_right_wheel` directions.
  - Uses angular command `0.3`.
  - Allows durations from `0.1` through `4.0` seconds.
  - Is preempted by gamepad activity.
- `scripts/diagnostics/motion-timed-turn.py`
  - Sends the diagnostic timed turn.

Measured rotation calibration:

```text
angular command: 0.3
estimated rotation rate: 55 degrees per second
both directions appear symmetrical
```

Current uncommitted work includes the production DoA reader, its tests, and
documentation updates. Do not discard it.

## Required Behavior

### Direction Conversion

Convert the stable raw DoA into a signed robot-relative angle:

```python
relative_degrees = ((raw_doa - 270 + 180) % 360) - 180
```

Interpretation:

```text
0: already facing the speaker
positive: turn toward the left drive wheel
negative: turn toward the right drive wheel
```

The valid signed range is `-180` through `180`. At the exact rear direction,
the formula returns `-180`; either turn direction is acceptable as long as it
is deterministic.

### Stable DoA Selection

Use these initial constants:

```text
poll interval: 0.1 seconds
stability duration: 0.5 seconds
stability tolerance: 5 degrees
stable cache maximum age: 2.0 seconds
already-facing tolerance: 15 degrees
```

Rules:

- Only consider readings where `speech_detected` is true.
- Do not collect candidate readings while `assistant_speaking` is true. The
  robot's own TTS playback must not replace the user's direction. Clear the
  in-progress candidate when assistant playback begins.
- Clear the in-progress candidate samples when speech detection becomes false.
  Keep the previously accepted stable cache until it expires.
- Accept a stable raw angle only after active-speech readings have remained
  within the circular five-degree tolerance for at least half a second.
- Use circular angle distance so `359` and `1` are considered two degrees
  apart.
- Cache the accepted raw angle and the monotonic time at which it was accepted.
- Do not average the entire utterance. Early readings were observed to wander
  significantly before settling.

A small stateful tracker in a new file such as `src/voice/doa.py` is appropriate
because it owns real sample history and cache state. Keep it limited to:

- recording readings,
- circular stability checks,
- exposing the current stable reading,
- converting raw DoA to signed relative degrees.

Do not put USB access in this tracker. `ReSpeakerDoA` remains the hardware
boundary.

### DoA Reader Lifecycle

`RobotVoiceService` should own the `ReSpeakerDoA` device and polling task.

Recommended lifecycle:

- After audio IO starts successfully in `start_orchestrator()`, try to open
  `ReSpeakerDoA`.
- Start an async polling task that calls the blocking `read()` through
  `asyncio.to_thread`.
- Poll while the voice orchestrator is armed or active, not only during an
  assistant turn.
- Pass whether assistant playback is active into the tracker update. Continue
  polling during playback, but do not accept those readings as user speech.
- A missing ReSpeaker control device or read failure must not stop ordinary
  voice operation.
- Log failures without flooding the log. Retry opening after a modest delay,
  such as one second.
- Cancel the polling task and close the device in `stop_all()`.

Keep this implementation simple. One polling coroutine owned by
`RobotVoiceService` is enough.

### Production Motion Intent

Add a production `face_me` intent to `src/control/motion_intent.py`.

Socket request:

```json
{"tool":"face_me","relative_degrees":85}
```

The bridge and executor must validate `relative_degrees` as a real number,
excluding booleans, within `-180` through `180`.

`robot-motion` owns the final turn calculation:

```python
duration_seconds = abs(relative_degrees) / 55
```

Behavior:

- If `abs(relative_degrees) <= 15`, complete immediately without wheel motion.
- Otherwise turn toward the left drive wheel for positive values.
- Otherwise turn toward the right drive wheel for negative values.
- Use the same angular command and physical direction signs already verified by
  `diagnostic_turn`.
- Keep the existing gamepad preemption behavior.
- Keep the four-second bounded-turn maximum. A 180-degree request should take
  about `3.27` seconds.
- Return the normal `{"ok": true, "result": "completed"}` response when done.

Do not expose direction or duration to the LLM. The LLM gets only a
parameterless `face_me` tool; `robot-voice` supplies the angle and
`robot-motion` derives direction and duration.

The existing diagnostic intent can remain for calibration. Reuse its constants
where sensible, but rename constants that are no longer diagnostic-only if that
makes the production code clearer.

### Assistant Tool

Add a strict, parameterless `face_me` tool in `src/voice/assistant.py`.

Suggested description:

```text
Turn the robot once to face the person who most recently spoke. Use when the
user asks the robot to face them, look at them, or turn toward them.
```

Tool execution should call a dedicated `face_me_caller`, not add special
parameter behavior to the existing generic `motion_intent_caller`.

The caller owned by `RobotVoiceService` should:

1. Read the current stable cached DoA.
2. Return `{"ok": false, "error": "speaker_direction_unavailable"}` if no stable
   reading exists.
3. Return `{"ok": false, "error": "speaker_direction_stale"}` if it is older
   than two seconds.
4. Convert the raw angle to signed relative degrees.
5. If within fifteen degrees, return a successful already-facing result without
   contacting motion, for example:

   ```json
   {"ok":true,"result":"already_facing","relative_degrees":1}
   ```

6. Otherwise call:

   ```python
   request_motion_intent(
       motion_socket,
       "face_me",
       timeout=5.0,
       relative_degrees=relative_degrees,
   )
   ```

Use a timeout longer than the existing generic motion timeout. A full turn can
take approximately `3.27` seconds. The motion bridge currently waits up to five
seconds, which is sufficient but should be kept in mind if turn limits change.

Thread the dedicated caller through:

```text
RobotVoiceService
  -> VoiceSession
  -> handle_scribe_events
  -> run_assistant_turn
  -> stream_openai_words
```

Follow the existing `camera_snapshot_caller` and `robot_inspection_caller`
patterns. Keep the function parameter optional so existing tests and callers
continue to work.

Update `config/operational_system_prompt.md` only if testing shows the model
does not use the tool reliably. The tool description should be sufficient for
the first implementation.

## Suggested Implementation Order

### 1. Add and Test the Pure DoA Tracker

Create `src/voice/doa.py`.

Add focused tests, preferably `tests/test_voice_doa.py`, covering:

- raw front `270` converts to relative `0`,
- raw left `0` converts to positive `90`,
- raw right `180` converts to negative `90`,
- rear-left `84` converts to approximately positive `174`,
- circular distance handles `359` and `1`,
- speech-false readings do not become candidates,
- speech-false clears candidate history,
- assistant playback clears candidate history and cannot update the cache,
- unstable samples do not update the stable cache,
- half a second of stable speech updates the cache,
- a previously stable cached reading survives silence,
- cache age is available for stale checks.

Use an injected monotonic timestamp in tracker methods. Do not sleep in tests.

### 2. Add the Production `face_me` Motion Intent

Modify:

```text
src/control/motion_intent.py
tests/test_motion_intent.py
tests/test_robot_motion.py
```

Tests should cover:

- socket validation accepts `-180`, `0`, and `180`,
- socket validation rejects missing values, strings, booleans, and out-of-range
  values,
- executor rejects invalid values,
- positive angle turns toward the left wheel,
- negative angle turns toward the right wheel,
- calculated duration uses 55 degrees per second,
- requests within fifteen degrees complete without movement,
- a 180-degree request remains below the four-second maximum,
- gamepad activity preempts `face_me`,
- `robot-motion` starts the parameterized production intent from the bridge.

Avoid duplicating validation logic more than necessary, but do not introduce a
large abstraction just to share two small checks.

### 3. Wire DoA Polling Into `RobotVoiceService`

Modify:

```text
src/robot_voice.py
tests/test_robot_voice.py
tests/test_robot_voice_wake.py
```

Add service fields for the reader, tracker, and polling task. Test with a fake
reader; no test should require USB hardware.

Tests should cover:

- successful readings are recorded in the tracker,
- read failures do not terminate voice service,
- unavailable USB does not prevent orchestrator startup,
- stop closes the reader and cancels the polling task,
- stale/unavailable cache produces the expected `face_me` error,
- fresh cache produces the expected signed angle and five-second motion call,
- already-facing cache does not contact motion.

### 4. Expose the Assistant Tool

Modify:

```text
src/voice/assistant.py
src/voice/session.py
tests/test_voice_core.py
tests/test_voice_session.py
```

Tests should cover:

- `ASSISTANT_TOOLS` includes `face_me`,
- the tool is strict and parameterless,
- a `face_me` function call invokes the dedicated caller,
- the caller result is returned to the model as function-call output,
- missing caller returns a clear unavailable error,
- `VoiceSession` passes the caller through to assistant handling.

Update the exact expected tool-name set in `tests/test_voice_session.py`.

### 5. Run Automated Verification

Run focused suites while implementing:

```bash
python3 -m unittest tests.test_respeaker
python3 -m unittest tests.test_voice_doa
python3 -m unittest tests.test_motion_intent
python3 -m unittest tests.test_robot_motion
python3 -m unittest tests.test_voice_core
python3 -m unittest tests.test_voice_session
python3 -m unittest tests.test_robot_voice
python3 -m unittest tests.test_robot_voice_wake
```

Then run:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
git diff --check
```

## Raspberry Pi Verification

After deploying and running `setup.sh`, verify permissions first:

```bash
.venv/bin/python scripts/diagnostics/respeaker-flex-python-control/respeaker_get_doa.py --interval 0.1
```

This should work without `sudo`.

Then verify the production behavior:

1. Place the robot on the floor with room to rotate and keep the gamepad ready.
2. Speak from the front and ask it to face you. It should report already facing
   or make no meaningful turn.
3. Speak from beside the left wheel and ask it to face you. It should turn
   toward the left wheel.
4. Repeat from beside the right wheel.
5. Repeat from behind the robot. It should make one bounded near-180-degree
   turn.
6. Move the gamepad during a turn. The turn must stop immediately and the tool
   should report preemption.
7. Have the robot speak for several seconds, then call `face_me`. Its own voice
   must not replace the last user direction.
8. Wait more than two seconds after speaking, then ask it to face you without
   speaking from a stable direction. It should report stale or unavailable
   speaker direction rather than turning on old data.
9. Unplug or deny access to the ReSpeaker control device. Voice conversation
   should continue working; only `face_me` should be unavailable.

Watch logs during testing:

```bash
journalctl -u robot-voice.service -f
journalctl -u robot-motion.service -f
```

## Acceptance Criteria

The implementation is complete when:

- Normal voice operation still works if DoA USB access is unavailable.
- Stable active speech produces a fresh cached raw DoA.
- Transitional and silence-cached DoA values do not become the selected
  direction.
- The robot's own TTS playback does not become the selected direction.
- `face_me` is parameterless from the model's perspective.
- Stale or unavailable direction never causes motion.
- The signed angle sent over the motion socket is bounded to `-180` through
  `180`.
- `robot-motion` derives direction and duration from the angle.
- A full turn is possible and remains below the four-second motion limit.
- Gamepad input preempts the turn.
- The robot performs one turn and does not chase DoA continuously.
- All automated tests pass.

## Known Follow-Up

Timed turns are open-loop and will vary with battery, floor friction, and robot
load. When the BNO085 IMU is installed, replace duration-based angle estimation
with heading feedback while keeping the same bounded `face_me` intent and
gamepad-preemption behavior.
