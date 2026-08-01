# Voice Code Review

Review of the voice stack (`robot_voice.py`, `voice/*`, `config/voice.py`,
`drivers/respeaker.py`). No code changes — findings only.

Overall the stack is unusually well-commented and testable, with clear file
ownership. Items below are ordered roughly by how much I'd care.

---

## Bugs

### 1. Goal-runner model calls are never metered

`_model_response` in `agent_runner.py` doesn't take or record `usage`, so every
`responses.create` inside an iterative goal — which runs at `reasoning effort
"medium"`, the expensive path — is invisible to `UsageTotals` and the cost
dashboard. The assistant path records usage on `response.completed`; the goal
loop records nothing.

```python
# agent_runner.py — _model_response
create_kwargs: dict[str, Any] = {
    "model": openai_model,
    "input": input_items,
    "reasoning": {"effort": AGENT_REASONING_EFFORT},
    ...
}
```

### 2. Scribe receive-task failures are swallowed with no log

If `receive_transcripts` dies (malformed JSON from the server, unexpected close),
`close_link` awaits the task inside `suppress(asyncio.CancelledError, Exception)`
and the actual exception evaporates. The main loop then reconnects silently. A
repeating server-side problem becomes an invisible reconnect churn — `scribe_last_error`
never gets set on this path either.

```python
# elevenlabs_io.py — close_link
with suppress(asyncio.CancelledError, Exception):
    if socket.receive_task is not None:
        await socket.receive_task
```

### 3. Session-task exception skips `_deactivate_session`

In `_run_orchestrator`, `_wait_for_session_end` re-raises the session task's
exception (`session_task.result()`), which kills the orchestrator task before
`_deactivate_session` runs. Recovery does happen — `_run_wake_orchestrator`
notices the dead task and does a full `stop_all()` — but that means any transient
session failure tears down the audio device, DoA reader, and wake model and
rebuilds everything, skipping the end chime and the normal "waiting" publish. A
try/finally around the deactivate would make failures degrade like normal session
ends.

### 4. `scan` degree handling is inconsistent with `move`'s

`move` carefully validates `distance_meters` (type, bool, finiteness), but `_scan`
does `abs(call.arguments.get("degrees") or 360.0)`, so `degrees=0` silently
becomes a full 360 sweep, and a non-numeric value raises `TypeError` that
propagates up and fails the whole turn/goal step instead of returning a tool
error. `turn` in `dispatch_tool` passes `**arguments` through with no
validation at all.

### 5. Mid-sentence TTS reconnect loses everything already sent

In `send_text_chunk`, when a send fails the socket is torn down and a fresh one
opened — but all text chunks previously sent to the dead socket are gone. The
robot speaks only the tail of the reply with no indication anything was dropped.
Might be acceptable, but worth knowing it speaks truncated sentences on a flaky
connection rather than restarting the line.

### 6. Goal narration/final speech has no timeout

`run_agent_goal`'s time budget only wraps model calls (the comment admits this
for tool dispatch), but `speak_final` → `speak_progress` → `speak_with_eleven_flash`
is also unbounded: `finish_voice_socket` awaits `play_task` forever if the server
never sends `isFinal`. The assistant path has the 120s turn timeout as a
backstop; a hung TTS at the end of a goal hangs the goal task indefinitely, and
the session idle timer won't fire because status is stuck at
"speaking"/`assistant_working=True`. Only a user barge-in commit rescues it.

### 7. Wake-event race at session end

`_run_orchestrator` clears `_wake_event` *after* `_deactivate_session` returns,
and `_deactivate_session` re-arms (`_mode = VOICE_ARMED`) before playing the end
chime. A wake that fires in that window gets its event cleared — the user hears
the wake chime but no session starts — and its `_wake_audio` is left populated,
so a later `talk_now` would replay that stale audio into Scribe. In practice
`detector.reset()` setting `last_fire_at` plus the 2s debounce mostly covers the
window, but it shrinks to zero if `wake_debounce_secs` is configured to 0.

```python
# robot_voice.py — _run_orchestrator
await self._wait_for_session_end()
await self._deactivate_session()
self._wake_event.clear()
```

### 8. Barge-in speculation drops the utterance prefix

In `handle_partial`, the non-playback path stitches `state.utterance_prefix` onto
the text before speculating, but the accepted-barge-in path starts
`start_after_stable_partial(text)` with the raw partial. So after a continuation
retraction, if the next fragment arrives while new playback is active and barges
in, the stitched first half is lost.

---

## Weird code / smells

- **Dead things.** `VoiceConfig.voice_id` is parsed and stored but never
  consumed anywhere (voices come from personality cards). `AudioLevels.mic_rms`
  is written at `assistant.py:1554` and never read. `DEFAULT_VOICE_ID` is
  imported in `session.py` and never used, and the constant itself is duplicated
  verbatim in `assistant.py` and `personality.py` (drift risk).
  `TurnPolicy.should_accept_barge_in` is only used by one test — the plan doc
  `2026-05-20 - dynamic-barge-in-gating.md` already says to remove it.
- **Duplicate import** in `robot_voice.py`: `from telemetry.messages import ...`
  appears at both line 24 and line 32.
- **`END_SESSION_UTTERANCES`** enumerates ~25 politeness permutations ("can you
  please end your sessions"). Stripping leading "please"/"can you"/"can you
  please" before the set lookup would collapse this to ~10 core phrases and catch
  variants the enumeration misses.
- **Repeated prefix-stitch block, third use.** The
  `first_half = turn.committed_text or turn.prompt` / `utterance_prefix = ...` /
  deadline dance appears three times (`retract_continuation`, the
  commit-continuation branch of `handle_partial`, and `handle_commit`) — that's
  the three-use threshold for extracting a tiny function, and the magic
  `now + 10.0` deadline is inline twice with no named constant.
- **Circular import worked around with lazy imports.** `elevenlabs_io` imports
  `AudioLevels`/`note_mic_chunk` from `assistant`, while `assistant` imports
  `speak_with_eleven_flash` from `elevenlabs_io` inside functions. Moving
  `AudioLevels` to a neutral module (it's really audio-level state, not turn
  logic) would untangle it.
- **`all([await send_chunk(...) for ...])`** in the pre-roll flush keeps calling
  `send_chunk` on every remaining buffer after the first failure (each returns
  `False` fast, but it's a strange shape for "stop on first failure").
- **`self.session.history.clear()`** in `_deactivate_session` is a no-op ritual
  — every `_activate_session` constructs a fresh `VoiceSession` with a fresh
  `ConversationHistory`.
- **`{"ok": True, ...}` for invalid JSON** on the command socket reads oddly;
  presumably `ok` means "service alive", but `ok: True, reason: invalid_json`
  will confuse a future caller.
- **Timeline latency stats can merge turns across sessions.** `next_turn_id`
  resets to 0 per session, and timeline events survive session end within the
  30s horizon, so two sessions back-to-back can collide `turn_id` keys in
  `latency_stats`. Cosmetic.
- **`handle_scribe_events`** is a 22-parameter passthrough that just constructs
  `TurnOrchestrator`. The session could construct the orchestrator directly.
- **Implicit clock coupling.** `AudioLevels.playback_at` is written with
  `loop.time()` but read in `_sample_timeline` against `time.monotonic()`. It
  works because asyncio's default clock *is* `time.monotonic`, but nothing states
  that assumption.

---

## Inefficiencies

- **Timeline publish cost.** `TimelineBuffer.snapshot` merges and sorts up to
  ~700 events, runs `latency_stats` over all of them, and rebuilds ~600 level
  samples into lists — 5×/second, plus the full `voice_update` construction and
  a fresh blocking Unix-socket `connect()+sendall()` per publish (bounded by the
  10ms timeout, but it's on the event loop). This is almost certainly the biggest
  steady-state CPU line item on the Pi; the profiling hooks suggest you know.
  Caching the sorted merge between event insertions, or only building the timeline
  when a dashboard is actually subscribed, would cut most of it.
- **Wake scoring bursts block the loop.** When the RMS gate opens after quiet,
  `_score_frames` runs up to 7 ONNX predicts back-to-back synchronously on the
  event loop (preroll + current frame). At Pi predict latencies that's a
  potential ~100ms+ stall for audio fan-out and playback during the exact moment
  the user starts talking.
- **`pcm16_rms` runs twice per frame** — once in `WakeWordDetector.check` and
  once in `stream_audio_to_scribe` for the same 80ms frame (different
  subscribers, same audio). Small, but it's per-frame forever.

---

## Improvements

- **Any config change nukes the world.** `same_orchestrator_config` only exempts
  `personality`, so nudging a barge-in slider or `session_idle_secs` on the
  dashboard tears down the audio streams, DoA reader, wake model, and any active
  session. Splitting fields into "restart-worthy" (devices, sample rate, wake
  model) vs. hot-appliable (policy knobs, idle secs) would make tuning during a
  conversation painless.
- **`_forward_clearances`** silently returns `None` on any exception with no
  log — unlike its sibling in `attach_motion_observation`, which logs. Worth one
  warning line for symmetry, since a broken telemetry socket currently just makes
  overlays disappear.
- **`scan` aborts mid-sweep on a failed turn** and leaves the robot at an
  arbitrary heading with no attempt to return to start; the error result doesn't
  tell the model how far it got, so the model's mental map of heading is now
  wrong (the pose tracker helps in the goal path, but the plain error output
  omits `degrees_covered`).

---

## Suggested priority

| Priority | Item | Why |
|---|---|---|
| 1 | Goal-runner usage metering (#1) | Silently wrong cost data |
| 2 | Scribe receive error logging (#2) | Hides exactly the class of failure you'd want in the field |
| 3 | Session exception → deactivate (#3) | Avoids full hardware teardown on transient session errors |
| 4 | Goal TTS timeout (#6) | Can hang a session indefinitely |
| 5 | `scan` argument validation (#4) | Crashes turn/goal on bad model input |
| 6 | Config hot-reload split | Makes dashboard tuning usable mid-conversation |
| 7 | Timeline publish cost | Pi CPU headroom |
