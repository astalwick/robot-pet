# Speech-Triggered Scribe

Goal: stop paying ElevenLabs Scribe for quiet audio without changing the
user-facing voice session semantics.

Today, an active voice session opens one Scribe websocket and sends every mic
frame. When local audio is below the upload threshold, `stream_audio_to_scribe`
sends zeroed PCM instead of real audio. That keeps Scribe's VAD/session context
simple, but it still sends audio minutes to Scribe and costs money.

New direction: keep the Hey Bloop conversation session separate from the Scribe
websocket lifecycle. The active conversation should behave as though Scribe is
always available, but the cloud socket should open only when useful.

## Fixed Decisions

- Hey Bloop session active/inactive is separate from Scribe open/closed.
- When Hey Bloop session is inactive, Scribe never opens.
- When Hey Bloop wakes a session, pre-open Scribe immediately.
- Hide Scribe pre-open behind the wake chime.
- Do not add wake-chime upload suppression. Rely on XVF3800 AEC and the existing
  local gates.
- During an active Hey Bloop session, open Scribe latency-first on the current
  low local threshold.
- Include local pre-roll when uploading starts, so first words are not clipped.
- Do not use Scribe `previous_text` in the first pass.
- Keep Scribe `commit_strategy=vad`.
- After local speech ends, send a short quiet tail and wait for Scribe commit.
- If commit does not arrive by timeout, close Scribe and do not promote
  uncommitted partials into committed input.
- Do not change speculative partial behavior in this project.
- During assistant speech, open Scribe on the same low local threshold. Existing
  barge-in policy still decides whether transcripts are trusted.
- Split local audio monitoring from Scribe transport.
- Reconnect Scribe mid-utterance if local speech is still active.
- Add minimal Scribe lifecycle telemetry.
- Keep new timing knobs as internal constants at first, not voice config fields.
- After a Scribe commit, hold the socket open briefly for quick follow-up speech.
- During hold-open grace, keep the socket open but do not upload quiet audio.
- Hey Bloop idle behavior remains semantically unchanged, as though Scribe were
  always open.
- STT cost accounting counts only audio actually uploaded to Scribe.
- Pre-open failure does not fail the active Hey Bloop session. Retry on speech.
- Idle Scribe socket closure is normal. Do not reconnect-loop just to keep an
  idle socket alive.
- Validation includes unit/fake-websocket tests plus a Pi smoke checklist.

## Existing Code Shape

Important files:

- `src/robot_voice.py`
  - Owns armed/active/disabled orchestration.
  - `_run_wake_loop()` currently plays wake chime, then sets `_wake_event`.
  - `_activate_session()` creates `VoiceSession`.
  - `_wait_for_idle()` and `publish()` own active session idle semantics.
- `src/voice/session.py`
  - `VoiceSession.start()` creates the mic frame iterator and starts two tasks:
    `stream_audio_to_scribe(...)` and `handle_scribe_events(...)`.
  - Keep this public wiring simple if possible.
- `src/voice/elevenlabs_io.py`
  - `stream_audio_to_scribe(...)` currently opens one Scribe websocket for the
    whole active session.
  - It computes RMS, emits `audio_activity`, updates `AudioLevels`, and sends
    either real PCM or zero PCM.
  - This is the main file to change.
- `src/voice/assistant.py`
  - `handle_scribe_events(...)` consumes `audio_activity`, `partial`, and
    `commit`.
  - It drives hearing/thinking/speaking statuses, speculation, commits, history,
    and barge-in.
  - Do not change speculation or barge-in policy in this project.
- `src/drivers/respeaker.py`
  - Mic capture is already continuous and local.
  - `mic_frames()` gives bounded subscriber queues.
  - Scribe can stop uploading without stopping local capture.
- `src/voice/usage.py`
  - STT cost should count uploaded audio seconds only after this change.
- `src/telemetry/messages.py`
  - Add minimal Scribe lifecycle fields here.

## Scribe API Notes

Scribe v2 Realtime is a websocket STT API.

Docs:

- `https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime`
- `https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/transcripts-and-commit-strategies`
- `https://elevenlabs.io/blog/introducing-scribe-v2-realtime`

Relevant behavior:

- Use websocket URL with `model_id=scribe_v2_realtime` and
  `audio_format=pcm_16000`.
- Keep `commit_strategy=vad` for this project.
- Input messages are `input_audio_chunk`.
- Scribe supports `previous_text`, but do not use it in this first pass.
- Partial transcripts are interim.
- Committed transcripts are stable.
- We should not invent a new path that turns timeout partials into committed
  user input.

## Internal Constants

Keep these as constants in `src/voice/elevenlabs_io.py` for the first pass.
Do not add config/dashboard fields until hardware data says a knob matters.

Suggested starting values:

- `SCRIBE_PREROLL_SECS = 0.5`
- `SCRIBE_POST_SPEECH_TAIL_SECS = 1.2`
- `SCRIBE_COMMIT_TIMEOUT_SECS = 2.0`
- `SCRIBE_HOLD_OPEN_SECS = 1.5`
- Open threshold remains `MIC_SCRIBE_SEND_RMS_MIN`.
- Existing `MIC_SCRIBE_GATE_HOLD_SECS` can still define local speech hold-open,
  or can be renamed if the new code reads more clearly.

These are not sacred. Keep them easy to find and adjust.

## Implementation Steps

### Step 1 - Add Scribe State Names

In `src/voice/elevenlabs_io.py`, add simple string constants or a small local
set of state names:

- `closed`
- `preopen`
- `uploading`
- `waiting_for_commit`
- `hold_open`
- `reconnecting`

Do not create a manager class unless the code becomes truly hard to follow.
This repo prefers simple functions and local state.

### Step 2 - Keep Local Audio Monitoring Always On

Change `stream_audio_to_scribe(...)` so it always drains `audio_chunks` while
the active voice session is running.

For every chunk:

1. Compute `rms = pcm16_rms(chunk)`.
2. Call `note_mic_chunk(audio_levels, rms)` when `audio_levels` is present.
3. Update `audio_levels.scribe_gate_open` based on the local upload/open gate.
4. Emit `{"type": "audio_activity", "rms": rms}` at the existing throttled
   interval.
5. Add the chunk to a pre-roll ring buffer.
6. Decide whether Scribe should open or begin uploading.

Important: local `audio_activity` must continue even when no Scribe socket is
open. This preserves Hey Bloop active-session behavior and dashboard/timeline
semantics.

### Step 3 - Add a Pre-Roll Buffer

Add a byte/frame buffer inside `stream_audio_to_scribe(...)`.

Use frame count, not wall-clock timers, because chunks are fixed 80 ms today:

- `MIC_BLOCKSIZE = 1280`
- sample rate is 16000
- one frame is about 0.08 seconds

Compute the max number of frames from `SCRIBE_PREROLL_SECS`.

When upload starts, send buffered pre-roll frames first, then live frames.
Do not count pre-roll in STT usage until it is actually sent.

### Step 4 - Split Websocket Open From Audio Upload

Currently the code opens websocket before entering the audio loop. Reverse that:
the audio loop should be primary, and websocket state should be optional.

Expected behavior:

- On active session start, start a background pre-open attempt.
- If pre-open succeeds, keep the socket available but do not upload quiet audio.
- If pre-open fails, record state/error and keep the active session alive.
- On local speech crossing threshold:
  - if no websocket exists, open one;
  - if opening fails, retry while local speech is still active;
  - once open, send pre-roll and live audio.

Avoid reconnecting forever when no speech is active.

### Step 5 - Receive Scribe Events While Socket Is Open

Keep a receive task for each open websocket.

For each received message:

- Parse JSON.
- Call `log_elevenlabs_payload(...)`.
- On non-empty `partial_transcript`, put `{"type": "partial", "text": text}`.
- On non-empty committed transcript, put `{"type": "commit", "text": text}` and
  mark that the current upload got a commit.

If the receive task fails:

- If local speech/upload is active, enter `reconnecting` and reopen.
- If the socket was idle/preopen/hold-open, treat it as normal closure unless
  the error is useful for telemetry.

### Step 6 - Upload Only Useful Audio

Do not send zero PCM during ordinary quiet.

Upload rules:

- When local speech crosses the low threshold:
  - send pre-roll frames;
  - send live speech frames while the local upload gate is open.
- When local speech ends:
  - send a quiet tail of zero PCM for `SCRIBE_POST_SPEECH_TAIL_SECS`;
  - wait for Scribe VAD commit or `SCRIBE_COMMIT_TIMEOUT_SECS`;
  - then enter hold-open or close.
- During hold-open grace:
  - keep the websocket connected;
  - do not upload quiet chunks;
  - if speech resumes, upload immediately without paying websocket handshake.
- After hold-open grace expires:
  - close the websocket;
  - set state to `closed`.

Only increment `usage.stt_audio_seconds` when sending a frame to Scribe.

### Step 7 - Commit Timeout Behavior

If Scribe has produced partials but no commit by the timeout:

- Close the socket.
- Do not put a synthetic `commit` event.
- Do not promote latest partial to committed input.
- Emit telemetry/timeline for commit timeout if practical.

This is deliberate. False partials are already a concern, and this project must
not add a second path from partial to committed turn.

### Step 8 - Preserve Assistant Event Semantics

Do not change `handle_scribe_events(...)` unless a tiny compatibility change is
required.

Specifically, do not change:

- speculative partial thresholds;
- commit decision policy;
- barge-in decision policy;
- assistant echo policy;
- conversation history behavior.

The new Scribe controller should keep producing the same event types:

- `audio_activity`
- `partial`
- `commit`

### Step 9 - Wake Chime and Pre-Open

Adjust `src/robot_voice.py` so session activation/Scribe pre-open can overlap
the wake chime.

Current behavior:

1. wake detected;
2. play wake chime;
3. set `_wake_event`;
4. orchestrator activates session.

Desired behavior:

1. wake detected;
2. start activation path promptly;
3. play wake chime without blocking Scribe pre-open;
4. keep active session semantics unchanged.

Do this carefully. The wake loop and orchestrator are separate today, and the
simple implementation may be to set `_wake_event` before or concurrently with
`audio.play_wav(config.wake_chime_path)`.

Do not add chime upload suppression. AEC covers it for this pass.

### Step 10 - Minimal Telemetry

Add optional fields to `voice_update(...)` in `src/telemetry/messages.py`:

- `scribe_state: str | None`
- `scribe_open_count: int | None`
- `scribe_last_error: str | None`

Thread these through `RobotVoiceService.publish(...)`.

`stream_audio_to_scribe(...)` already receives `audio_levels`, but not a status
callback. The simplest path is probably to add a small optional callback
parameter to `stream_audio_to_scribe(...)`, passed from `VoiceSession.start()`,
that publishes Scribe status updates through the existing session status
callback.

Keep status updates sparse:

- state changes;
- open count increments;
- last error changes.

Do not spam telemetry for every chunk.

### Step 11 - Timeline Events

Use existing `event_callback` path if easy.

Useful event types:

- `scribe_open`
- `scribe_close`
- `scribe_reconnect`
- `scribe_commit_timeout`

If adding timeline events makes the streamer signature messy, skip timeline
events and keep only voice telemetry fields. Telemetry is required; timeline is
nice to have.

### Step 12 - Tests

Add focused fake-websocket tests in `tests/test_elevenlabs_io.py`.

Required tests:

1. Quiet audio emits `audio_activity` but sends no audio chunks to Scribe.
2. Active session pre-open attempts to connect.
3. Pre-open failure does not stop the streamer.
4. Threshold crossing opens Scribe when needed.
5. Threshold crossing sends pre-roll before live audio.
6. Usage counts only bytes actually sent over websocket.
7. Speech end sends quiet tail.
8. Commit after tail is forwarded as `{"type": "commit", "text": ...}`.
9. Commit timeout closes without creating a synthetic commit.
10. Post-commit hold-open does not upload quiet audio.
11. Speech during hold-open uploads without a new connect.
12. Idle socket close is treated as normal.
13. Mid-speech websocket failure reconnects and continues while local speech is
    still active.

Add or adjust tests in `tests/test_robot_voice_wake.py`:

1. Wake activation can start before wake chime completes.
2. Pre-open failure does not fail active session startup, if this is exposed at
   the `RobotVoiceService` level.
3. Idle behavior is unchanged when Scribe opens/closes internally.

Add telemetry tests in `tests/test_telemetry_messages.py` for new optional
fields.

Run:

```bash
python3 -m unittest tests.test_elevenlabs_io
python3 -m unittest tests.test_telemetry_messages
python3 -m unittest tests.test_robot_voice_wake
python3 -m unittest tests.test_voice_core
python3 -m unittest tests.test_voice_session
python3 -m unittest tests.test_voice_usage
```

Then run the broader voice set if time permits:

```bash
python3 -m unittest tests.test_elevenlabs_io tests.test_voice_core tests.test_voice_session tests.test_robot_voice tests.test_robot_voice_wake tests.test_voice_usage tests.test_telemetry_messages
```

## Pi Smoke Checklist

Run this on the robot after unit tests pass.

1. Start `robot-voice`.
2. Say "Hey Bloop".
3. Confirm wake chime plays.
4. Immediately speak a normal request after the chime.
5. Confirm first request does not feel slower than current behavior.
6. Stay quiet inside the active session.
7. Confirm Scribe telemetry moves to closed/idle and STT cost stops climbing.
8. Speak again before Hey Bloop session idle timeout.
9. Confirm Scribe opens and the session continues without requiring another
   wake word.
10. Try quick follow-up speech after a committed turn.
11. Confirm it does not feel like it pays a fresh connection delay.
12. During TTS, say "stop" or "wait".
13. Confirm barge-in still works.
14. Let the session idle out.
15. Confirm Hey Bloop session closes exactly as it did before this change.
16. Check logs for reconnect churn during quiet. There should not be repeated
   reconnect attempts just to keep an idle Scribe socket alive.

## Non-Goals

- Do not change speculative partial policy.
- Do not tune barge-in thresholds.
- Do not add Scribe timing fields to `VoiceConfig` or dashboard.
- Do not use `previous_text`.
- Do not add a new audio abstraction layer.
- Do not turn this into a generic STT provider framework.
- Do not make Scribe socket closure end the Hey Bloop session.

## Acceptance Criteria

- Quiet active sessions no longer upload continuous zero PCM to Scribe.
- STT cost reflects only uploaded audio.
- First utterance after Hey Bloop does not noticeably regress in latency.
- Normal turns still produce partials/commits and assistant responses.
- Quick follow-up speech remains natural.
- Barge-in still works during assistant speech.
- Hey Bloop idle behavior is unchanged.
- Unit tests cover the new Scribe lifecycle.
- Pi smoke checklist passes.
