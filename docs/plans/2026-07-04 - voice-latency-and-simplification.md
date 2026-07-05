# Voice Latency & Simplification

Goal: cut end-of-speech → first-audio latency on the main conversation path,
fix a handful of real bugs found during review, and shrink/reshape the voice
code so the next round of work is cheaper. Behavior changes are confined to
Stages 1–4 and 7; Stages 5–6 must be behavior-preserving.

Style rules: this repo is allergic to enterprise code. Read `CLAUDE.md` before
starting and follow it. Prefer deleting code over adding it. If a step here
seems to require breaking a CLAUDE.md rule, stop and ask.

Run tests with `python3 -m unittest tests.test_voice_core` (plus the suites
named per stage). Line numbers are anchors as of 2026-07-04 — re-check with
grep before editing.

Each stage below is self-contained: it names its files, its steps, its tests,
and its done-when. Stages can be handed off independently, respecting the
dependency notes.

## Overview

| Stage | What | Effort | Value | Depends on |
|---|---|---|---|---|
| 1 | Lower Scribe VAD silence threshold + measure | S | ★★★★★ | none |
| 2 | Speak text produced alongside tool calls | M | ★★★★★ | none (before 6) |
| 3 | TTS first-audio: earlier prewarm, chunk schedule, no word batching | S | ★★★★ | after 2 (same function) |
| 4 | Small bug sweep: scan, latency stats, output latency | S | ★★ | none |
| 5 | Dead code removal + move schemas out of assistant.py | M | ★★★ | none (before 6) |
| 6 | Extract handle_scribe_events into a class | L | ★★★★ | 2, 5 |
| 7 | Faster barge-in gate + decouple from telemetry sampler | S–M | ★★★ | none (easier after 6) |
| 8 | Instant playback duck on local speech | — | — | **discussion first — do not implement** |

Explicitly out of scope (decided 2026-07-04): persistent multi-context TTS
websocket, semantic turn-detection model, cross-session memory. The first two
are natural follow-ons if Stages 3 and 1 don't measure well enough.

---

## Stage 1 — Lower the Scribe VAD silence threshold

**Effort S / Value ★★★★★.** The single biggest latency lever. Total response
time on the speculative path is roughly `vad_silence_threshold_secs` + ~0.3s,
because playback only opens on Scribe's commit and the commit waits out the
silence threshold. Dropping 1.0 → 0.7 takes ~0.3s off every single turn.

The plumbing already exists — `vad_silence_threshold_secs` is a config field
(`src/config/voice.py:42`), passed through `VoiceSession.start()`
(`src/voice/session.py:144`) into the Scribe URL params
(`src/voice/elevenlabs_io.py:125`). This stage is a default change plus a
measurement protocol.

Steps:

1. Change the default from `1.0` to `0.7` in `VoiceConfig`
   (`src/config/voice.py:42`). Check the `VOICE_FIELDS` entry (~line 251) —
   if it embeds a default or a range, keep them consistent (range should
   allow at least 0.5–2.0).
2. Grep `tests/` for `vad_silence_threshold_secs` — fix any test pinning the
   old default. Note: a saved `config/voice` JSON on the robot will override
   the default; mention in the commit message that the robot's saved config
   needs the field updated (or cleared) for the new default to take effect.
3. On-robot measurement (this is most of the work):
   - Before changing anything, capture a baseline: a few conversations, then
     record `median_input_to_audio_ms` from the timeline latency snapshot
     (`TimelineBuffer.latency_stats`, `src/robot_voice.py:122`) and the
     `false_starts` counter from the status stream.
   - Repeat at 0.7. If the robot starts talking over mid-sentence thinking
     pauses noticeably more (watch `false_starts` and turn-cancel events with
     reason `continuation_retraction`), step back up to 0.8.
   - Record both numbers in this doc under a "Results" note when done.

Tests: `python3 -m unittest tests.test_voice_config tests.test_voice_session`.

Done when: default is lowered, tests pass, and before/after latency +
false-start numbers are recorded.

---

## Stage 2 — Speak text produced alongside tool calls

**Effort M / Value ★★★★★.** Today `stream_openai_words`
(`src/voice/assistant.py:889`) buffers each response's text into
`response_text_chunks` (reset at line 931, top of each `while True`
iteration) and only yields it `if not function_calls` (line 997). Any text
the model produces in the same response as a function call is **silently
dropped** — except `start_goal`, which carries it as `preamble` (line 1020).

Consequences today:

- "Sure, backing up!" alongside a `move` call is never spoken; the robot
  moves in silence and the acknowledgement is lost from history too.
- The system prompt (`config/operational_system_prompt.md`) tells the model
  to say a sign-off *then* call `end_session`. The sign-off is dropped, the
  loop at line 1088 sees `text_streamed` false, does a **whole extra LLM
  round trip** to regenerate a goodbye. Fixing this deletes a full LLM
  round from every session end.

The fix — yield text eagerly instead of buffering per-response:

1. In the `response.output_text.delta` handler (lines 958–974), yield each
   batched chunk directly instead of appending to `response_text_chunks`
   (i.e. `yield "".join(word_buffer[:3])` where it currently appends). Same
   for the end-of-stream flush at lines 990–995. Set `text_streamed = True`
   at the yield sites. Delete `response_text_chunks`.
2. `if not function_calls:` (line 997) becomes just `return` — text already
   went out.
3. Goal handoff (line 1016): the preamble text has now already been yielded
   and spoken, so yield `AgentGoalRequest(goal=..., preamble="")` and update
   the comment. Follow `preamble` into `run_agent_goal`
   (`src/voice/agent_runner.py`) and verify empty preamble is a no-op (it
   should be — the no-text-before-goal case always produced `""`). If the
   agent runner *speaks* the preamble via its own path, confirm nothing
   double-speaks now.
4. `run_assistant_turn` (line 1098): the first-chunk peek (line 1145) and
   `captured_openai_words` still work unchanged — when the model's first
   output is the goal call, the only yield is the `AgentGoalRequest` and the
   TTS socket never opens, exactly as today. Verify, don't assume: the
   ordering guarantee is that text deltas yield before function-call
   processing (they arrive before the stream completes).
5. End-session flow: with the sign-off now streamed, the check at line 1088
   (`text_streamed or end_session_tool_output_sent`) returns immediately
   after the `end_session` tool executes. Verify with a test: a response
   containing text + `end_session` speaks the text and does **not** issue a
   second `responses.create` call.
6. Sanity-check the interaction points that now receive tool-adjacent text:
   - `on_assistant_chunk` feeds echo suppression (`recent_assistant_text`) —
     spoken acknowledgements *should* be echo-suppressed, so this is correct.
   - `assistant_chunks` feeds history — acknowledgements now land in history,
     which is a fidelity improvement, not a regression.
   - Motion gating is untouched: motion tools still await `playback_event`
     (line 1031), so a speculative turn that gets retracted still never moves
     the robot, even though its acknowledgement text was queued to TTS
     (playback was gated, so nothing was audible).

Tests (`tests.test_voice_core`, grep for tests driving `stream_openai_words`
or asserting turn text around tool calls):

- Text + function call in one response → text is yielded/spoken and the tool
  still executes.
- Text + `end_session` → text spoken, exactly one `responses.create` call.
- `start_goal` as first output → no text yielded, `AgentGoalRequest` returned
  from the peek, TTS untouched.
- Text then `start_goal` → text spoken, `AgentGoalRequest` has empty
  preamble, goal still starts.

Done when: the robot acknowledges out loud while executing tools, session
end costs one LLM round, and the full `test_voice_core` suite passes.

On-robot smoke: "back up a little" → hear an acknowledgement roughly *while*
it moves. "goodbye" → sign-off plays, session ends, noticeably faster than
before.

---

## Stage 3 — TTS first-audio package

**Effort S / Value ★★★★.** Four small changes that each shave tens of
milliseconds off first-token → first-audio. Do this **after Stage 2** — (a)
and (c) touch the same functions Stage 2 rewrites.

1. **Prewarm the TTS socket before the first LLM token.**
   `speak_with_eleven_flash` already prewarms its websocket the moment the
   coroutine starts (`src/voice/elevenlabs_io.py:566` calls
   `prewarm_voice_socket()` before consuming any chunk). But
   `run_assistant_turn` (`src/voice/assistant.py:1145`) awaits the first LLM
   chunk **before** invoking the speaker — so the TLS+websocket handshake to
   ElevenLabs is serialized after LLM time-to-first-token instead of
   overlapping it. Restructure: invoke the speaker immediately and move the
   first-chunk peek inside `captured_openai_words`. The goal-handoff case
   changes shape: the generator yields nothing (it captures the
   `AgentGoalRequest` into `goal_request`), the speaker's
   `finish_voice_socket` sees `ws is None` and closes the prewarm socket.
   Cost: one wasted websocket handshake on goal handoffs — rare and
   harmless. If the peek's early-return is doing more than TTS avoidance
   (re-read the comment at lines 1140–1143), preserve that behavior.
2. **Lower the first generation chunk.** `chunk_length_schedule` is
   `[120, 160, 250, 290]` (`src/voice/elevenlabs_io.py:437`) — ElevenLabs
   waits for ~120 chars before synthesizing the first audio. Change to
   `[50, 120, 250, 290]` (50 is the API minimum). Slightly choppier first
   phrase is the trade; Flash is fast enough that this is the standard
   low-latency setting.
3. **Drop the 3-word batching.** In `stream_openai_words`, words accumulate
   until 3 are buffered before yielding (assistant.py lines 971–973 pre-Stage
   2). Yield each whitespace-delimited piece as soon as it's complete — the
   TTS socket does its own buffering via the chunk schedule; batching on our
   side only adds latency. (After Stage 2 these are direct yields; just
   remove the `>= 3` accumulation.)
4. **Cache the SSL context.** `ssl_context()`
   (`src/voice/elevenlabs_io.py:77`) builds a fresh `SSLContext` (CA load,
   etc.) on every connect (lines 190, 427). Build once at module level (or
   `functools.cache` on the function) and reuse.

Tests: `tests.test_elevenlabs_io` (chunk schedule / socket lifecycle
assertions), `tests.test_voice_core` (turn flow with the restructured peek).
While in there: `try_trigger_generation` (line 531) is deprecated by
ElevenLabs — remove it and confirm tests still pass.

Done when: timeline `first_token_to_audio` (see `latency_stats` fields,
`src/robot_voice.py:122`) improves measurably on-robot and all suites pass.

---

## Stage 4 — Small bug sweep

**Effort S / Value ★★.** Three independent fixes; no design questions.

1. **`_scan` wastes its last turn and never photographs the far end**
   (`src/voice/tools.py:310`). For a partial sweep, e.g. `degrees=180` with
   `SCAN_STEP_DEGREES=90`: `captures = round(180/90) = 2`, snapshots at 0°
   and 90°, then the loop turns to 180° **after the final capture** — that
   heading is never photographed — then the cleanup turns all the way back.
   The view at 180° was the whole point of asking for a 180° scan.
   Fix by computing the capture headings up front: full 360° sweep →
   `[0, step, ..., 360-step]` (unchanged behavior — turning after the last
   capture *is* the return to start); partial sweep → `[0, step, ..., total]`
   inclusive, turning only *between* captures, then return the short way.
   Keep the label math ("degrees to your left") true for the new headings.
   Test: partial sweep captures the far-end heading and issues no wasted
   turn; full sweep behavior unchanged (`tests/` — grep for `_scan` or
   `scan` tool tests).
2. **`latency_stats` misses stitched prompts**
   (`src/robot_voice.py:122–137`). A `turn_start` is matched to its input by
   *exact* prompt/text equality. Turns whose prompt was stitched with an
   `utterance_prefix` (the continuation-retraction path) never match, so the
   messiest, most latency-interesting turns are silently excluded from the
   median. Fix: fall back to a suffix match (`prompt.endswith(event_text)`)
   when no exact match is found. Test in `tests.test_robot_voice`.
3. **Output stream latency (optional, on-robot only).**
   `src/drivers/respeaker.py` (~line 281) opens the playback stream with
   `latency="high"`. Try `"low"` on the robot; keep it only if a long TTS
   playback has zero underruns/glitches. If it glitches at all, revert and
   note it here — the Pi may genuinely need "high".

Done when: both code fixes have tests and pass; the latency experiment has a
recorded verdict either way.

---

## Stage 5 — Dead code removal + move tool schemas out of assistant.py

**Effort M / Value ★★★.** Behavior-preserving. Shrinks `assistant.py` by
roughly a third and kills the lazy circular imports. Do this immediately
before Stage 6 — it exists to make that extraction smaller.

1. **Delete the VoiceSwitch machinery end to end.** It is dead in
   production: nothing constructs `VoiceSwitch` outside tests (verified by
   grep 2026-07-04). Remove:
   - `assistant.py`: `ALTERNATE_VOICE_ID` (line 25), `class VoiceSwitch`
     (607), `VoiceState.alternate_voice_id` + `set_voice` (649–660), and
     `VoiceSwitch` in the two `AsyncIterator[str | VoiceSwitch]` annotations
     (903, 1152).
   - `elevenlabs_io.py`: the import (line 17), the `isinstance(chunk,
     VoiceSwitch)` branch (568–572), the annotation (377).
   - `session.py`: import (line 14) and the `alternate_voice_id=` wiring (86).
   - `config/voice.py`: the `alternate_voice_id` field (39), its
     `from_values` line (76), and any `VOICE_FIELDS` entry. Confirm
     `from_values` ignores the stale key in saved config files (it reads keys
     explicitly, so it should).
   - Tests: `test_elevenlabs_io.py:244` (the voice-switch test — delete),
     `test_voice_session.py:84,93`, `test_voice_config.py:112–120` (rewrite
     to drop the field).
2. **Delete `AudioLevels.mic_last`** (assistant.py:88) and its write in
   `note_mic_chunk` (104) — grep first to confirm it is still never read.
3. **Deduplicate `pcm16_rms`.** It exists in `src/voice/turn_policy.py:266`
   and `src/voice/wakeword.py:19`. Canonical copy: `turn_policy`
   (`elevenlabs_io` already imports it from there). Make `wakeword.py`
   import it; delete its local copy.
4. **Delete the test-only module-level wrappers in `turn_policy.py`**
   (lines 254+: `normalized_transcript`, `transcript_matches`,
   `should_speculate`, `should_accept_barge_in`). Their only consumers are
   tests (`test_voice_core.py:50`); update those tests to call the
   `TurnPolicy` methods on a policy instance instead.
5. **Move the tool schema dicts out of `assistant.py`.** The ~220 lines of
   schemas (`EXPRESS_TOOL` … `WEB_SEARCH_TOOL`, lines 171–394) and the tool
   name constants (33–44) belong in `src/voice/tools.py`, which already owns
   `ROBOT_TOOLS`/`ASSISTANT_TOOLS`/`AGENT_TOOLS` and currently imports them
   *back* from assistant (tools.py:25). Move them; then check whether
   `tools.py` still needs anything from `assistant` — if not, the lazy
   `from voice.tools import ...` inside `stream_openai_words`
   (assistant.py:904) and friends can become normal top-level imports.
6. **Move snapshot interpretation out of `assistant.py`.** Lines ~397–604
   (`_interpret_sensor_reading`, `forward_clearances`,
   `forward_sensors_sentence`, `inspect_robot_snapshot`,
   `check_health_snapshot`, `check_surroundings_snapshot`) are tool-result
   shaping, not turn orchestration. Move them to `tools.py` next to their
   consumers (tools.py already lazily imports several of them — grep). Fix
   all importers (`agent_runner.py`, tests).
7. Optional, judgement call: `END_SESSION_UTTERANCES` (assistant.py:48) is
   25 hand-written permutations ("can you please end the session", ...). If
   a comprehension over (prefix × phrase) reads *more* clearly, collapse it;
   if it reads like clever code, leave it alone.

Tests: full suite — `python3 -m unittest discover tests`. This stage must
not change any test's asserted behavior except deletions for removed
features.

Done when: `assistant.py` contains no tool schemas, no snapshot
interpretation, no VoiceSwitch; no lazy imports remain that existed only to
break the assistant↔tools cycle; full suite green.

---

## Stage 6 — Extract `handle_scribe_events` into a class

**Effort L / Value ★★★★.** `handle_scribe_events`
(`src/voice/assistant.py:1199–2001`) is an ~800-line function holding ~25
closures over `TurnRuntimeState` plus loose nonlocals (`hearing_on`,
`thinking_on`, `user_speech_on`, `recent_assistant_text`, ...). Every change
to turn logic lands inside it. This is the maintainability keystone — and a
CLAUDE.md-sanctioned stateful class: it owns real lifecycle and concurrency
state.

**Do after Stages 2 and 5** (both rewrite parts of this file; land them
first). Nothing else should touch `assistant.py` while this is open.

Approach — mechanical, zero behavior change:

1. Create a class (suggested name `TurnOrchestrator`) in `assistant.py`
   whose `__init__` takes exactly what `handle_scribe_events` takes today.
   `TurnRuntimeState` stays as-is and becomes `self.state`; the loose
   nonlocals become instance attributes.
2. Convert each closure to a method with the same name (`handle_partial`,
   `handle_commit`, `start_turn`, `cancel_active_turn`,
   `publish_barge_in_state`, `emit`, `status`, ...). Do not rename, do not
   reorder logic, do not "improve" anything on the way through — every diff
   hunk should be explainable as "closure → method".
3. `handle_scribe_events` remains as a thin wrapper (construct the
   orchestrator, run its event loop) so `session.py` and the tests keep
   their entry point.
4. Before starting, check test coverage of the scariest paths — goal
   handoff/finish, continuation retraction, barge-in during playback,
   shutdown cleanup (the `finally` at line 1995). If any of those has no
   test, write a pinning test *first*, against the current code.

Tests: full `tests.test_voice_core` + `tests.test_voice_session` — they are
the safety net; the diff to them should be near-zero (only patch-target
paths if tests reach into closures, which they can't — so likely zero).

Done when: no function in `assistant.py` exceeds ~150 lines, the suite is
green with near-zero test diffs, and a reader can find "what happens on
commit" by looking at a method list.

---

## Stage 7 — Faster barge-in gate + decouple it from the telemetry sampler

**Effort S–M / Value ★★★.** Two coupled changes. Keeps transcript-confirmed
cancellation exactly as-is — this only makes the *existing* RMS gate react
sooner and unhooks it from the dashboard sampler.

Background: mic chunks are 1280 samples / 80ms (`MIC_BLOCKSIZE`,
`src/drivers/respeaker.py:16`). The Scribe uploader emits an
`audio_activity` event at most every 0.35s
(`LOCAL_SPEECH_LOG_INTERVAL_SECS`, `src/voice/elevenlabs_io.py:32`, used at
line 280). The barge-in gate needs RMS ≥ threshold *sustained* 350ms
(`update_near_end_gate`), and `state.gate_open` — the value barge-in
decisions actually read (`decide_barge_in_during_playback`,
assistant.py:852) — is only refreshed by `publish_barge_in_state`
(assistant.py:1286) when an `audio_activity` event arrives. Net: worst-case
detection is ~0.35s throttle + 0.35s sustain ≈ 700ms of talking before the
gate opens. Meanwhile `_sample_timeline` (`src/robot_voice.py:909`) *also*
calls `refresh_barge_in_gate` at 20Hz as a side effect of drawing the
dashboard — telemetry code that is load-bearing for conversation behavior.

Steps:

1. In `stream_audio_to_scribe` (elevenlabs_io.py:280), emit `audio_activity`
   on **every** chunk — delete the `LOCAL_SPEECH_LOG_INTERVAL_SECS` throttle.
   That's ~12.5 events/sec on an asyncio queue; trivial load.
2. In the `audio_activity` handler (assistant.py:1956–1988) /
   `publish_barge_in_state` (1286): keep refreshing the gate on every event,
   but throttle the `status(**barge_in_telemetry(...))` publish back to
   ~0.35s — the gate math should run at chunk rate, the dashboard spam
   should not. Simplest shape: split `publish_barge_in_state` into
   refresh (every event) + publish (time-throttled), tracked by a
   last-published timestamp on the orchestrator/state.
3. Remove the `refresh_barge_in_gate` call from `_sample_timeline`
   (robot_voice.py:921–927) — the sampler still reads
   `levels.gate_open`/`threshold_rms` for the timeline row, and still
   owns the `mic_peak` read-and-reset (that's genuinely telemetry). After
   this, grep for remaining `refresh_barge_in_gate` callers: the assistant
   event handler should be the only one.
4. The `max(int(event.get("rms", 0)), levels.mic_peak)` at assistant.py:1959
   existed to catch peaks between throttled events. With per-chunk events
   the chunk RMS is the real signal — drop the `mic_peak` term (verify no
   test pins it).
5. Re-check `update_scribe_upload_gate` and the wake-word RMS gate are
   untouched — this stage is about the *barge-in* gate only.

Tests: `tests.test_voice_core` barge-in tests (grep `barge_in`) — some may
encode the 0.35s cadence or feed synthetic `audio_activity` events; update
cadence assumptions, keep behavioral assertions. `tests.test_robot_voice`
for the sampler change.

Done when: with the robot speaking, sustained speech opens the gate in
~350–450ms (was ~700ms) — observable in the timeline gate row — and
`_sample_timeline` no longer mutates gate state.

---

## Stage 8 — Instant playback duck — **discussion first, do not implement**

**Deliberately not planned.** The review recommended pausing/ducking
playback within ~100ms of detected local speech (what most commercial
assistants do), but a pure RMS trigger is exactly the thing that falls apart
in a noisy room — kitchen clatter, a TV, a second conversation would stutter
playback constantly. Arlen explicitly wants a design discussion before any
of this is built.

Agenda for that discussion:

1. **Trigger signal.** The XVF3800 already reports `speech_detected`
   (`DoAReading.speech_detected`, `src/drivers/respeaker.py:77`) — a
   post-AEC hardware voice-activity flag that is far more noise-robust than
   raw RMS. Candidate trigger: `speech_detected AND rms floor`, rather than
   RMS alone. Verify its latency and false-positive rate on-robot first
   (log it against the timeline during normal use — cheap experiment, could
   piggyback on Stage 7 validation).
2. **Duck vs. pause.** Duck (-12dB and keep playing, restore if no
   transcript confirms) is much less jarring on false triggers than a hard
   pause; a hard pause needs a resume story.
3. **Resume policy.** If the transcript never confirms an interruption
   (it was the dog), how and when does playback recover, and does the
   half-ducked sentence replay or continue?
4. **DoA gating.** The DoA angle could reject "speech" from the direction
   of a known noise source (TV) — worth it, or speculative generality?

Prerequisites if/when green-lit: Stage 7 (gate plumbing), realistically
Stage 6 (this adds a playback state to the turn machine).
