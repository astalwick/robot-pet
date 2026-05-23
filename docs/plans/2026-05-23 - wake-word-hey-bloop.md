# Wake Word — "Hey Bloop" Plan

Goal: add local wake-word detection on the Pi so the robot can sit in a low-cost dormant state, hear **"Hey Bloop"**, play a short chime, and only then start the existing cloud voice session.

This plan is split into three phases on purpose:

- **Phase 0:** shared-audio refactor in `ReSpeakerAudio`. No wake word, no new features. Success = today's voice assistant works identically on the refactored capture/playback path.
- **Phase A:** wake word works **without** `VoiceSession`, Scribe, OpenAI, or TTS. Success = phrase spoken → model fires → chime plays.
- **Phase B:** wire Phase A into `robot-voice` so wake starts and idle ends the existing conversation loop.

Do not start Phase A until Phase 0 is proven on the real ReSpeaker hardware. Do not start Phase B until Phase A is proven on the real ReSpeaker hardware.

Related docs:

- [Personality Phase 1](../phases/personality-phase-1.md)
- [Voice assistant / ReSpeaker plan](2026-05-16%20-%20voice-assistant-respeaker.md)

## Why Phase A Comes First

The full voice stack already works when `voice.json` `enabled` is true, but it is **always hot**: mic audio streams to ElevenLabs immediately. Wake word needs the opposite default — listen locally, stay quiet in the cloud.

Phase A isolates three risky pieces:

1. Custom **"hey bloop"** model quality on real room noise.
2. **openWakeWord** runtime on the Pi 5.
3. **ReSpeaker audio ownership** — one process must own the USB mic and speaker without fighting PortAudio/ALSA.

If Phase A only works when `robot-voice` is stopped, Phase B will not work either.

## Phase 0 — Shared Audio Refactor

### Goal

Move `ReSpeakerAudio` from per-call streams to one process-lifetime input stream + one process-lifetime output stream with subscriber fan-out. **The voice assistant must behave identically before and after.** Wake word is not in scope for this phase; the only consumer is `VoiceSession`, just rewired to use the new API.

### The problem today

`ReSpeakerAudio` in `src/drivers/respeaker.py` is the right boundary, but capture and playback are opened **per use**:

- `microphone_chunks()` opens a new `sounddevice.RawInputStream` for each async iterator.
- `begin_playback()` opens a new `sounddevice.RawOutputStream` for each TTS turn.

That is fine while only `VoiceSession` uses the driver. It breaks when wake word and voice both need the mic:

- Two input streams on `hw:0,0` / `XVF3800` → typical failure: device busy, silent capture, or flaky reopen after close.
- Competing output streams → chime and TTS can stomp each other.

```text
Today (fragile if duplicated):

  VoiceSession --> microphone_chunks() --> NEW input stream
  VoiceSession --> begin_playback()     --> NEW output stream

Wake loop    --> microphone_chunks() --> SECOND input stream  (bad)
```

### Target: one owner, many consumers

`robot-voice` (one process, one `ReSpeakerAudio` instance) should hold **at most one input stream and one output stream** for the lifetime of the armed service.

```text
robot-voice.service
  ReSpeakerAudio (sole PortAudio owner)
    |
    +-- shared capture task
    |     +-- fan-out mono 16 kHz frames
    |           +-- WakeWordDetector (Phase A/B, dormant)
    |           +-- Scribe stream (Phase B, active only)
    |
    +-- shared playback path
          +-- wake chime WAV (Phase A/B)
          +-- ElevenLabs Flash PCM (Phase B, active)
```

Rules:

1. **Only `ReSpeakerAudio` opens sounddevice streams.** Wake and voice code consume PCM bytes; they do not call `sounddevice` directly.
2. **One capture loop** reads 6-channel interleaved PCM, extracts the configured channel, applies `input_gain`, publishes mono frames to subscribers.
3. **One playback path** serves chime and assistant speech through the existing playback queue / lock. Chime and TTS must not run concurrently; use `_playback_lock` or an explicit "playback owner" flag.
4. **Subscribers are cheap; streams are not.** Wake word scores frames in-process. Scribe receives the same frames only while the session is active.
5. **No second systemd service** for wake word on the same ReSpeaker. A separate `robot-wakeword.service` would race for the USB device unless you add IPC and a single audio daemon — out of scope for v1.

### Refactor sketch

Extend `ReSpeakerAudio` with shared lifecycle, without a generic audio bus:

- `async def start_io(self, stop_event)` — opens input (and optionally output) once.
- `async def stop_io(self)` — closes streams cleanly.
- `async def mic_frames(self) -> AsyncIterator[bytes]` — fan-out from the single capture task; one async iterator per subscriber, backed internally by a bounded queue (per-subscriber drop policy described below).
- `async def play_pcm(self, pcm: bytes)` or `async def play_wav(self, path: str)` — one-shot local playback for the chime using the shared output stream.

Output stream lifecycle: today the `RawOutputStream` is opened inside `_run_playback` and torn down when `_finish_playback` exits the `with stream:` block, so the device closes between every playback. After the refactor, the output stream stays open for the life of `start_io` / `stop_io`. `begin_playback` / `end_playback` (or `play_wav`) become queue lifecycle operations only — they do not open or close the PortAudio device. This is what makes a Phase A chime and a Phase B TTS playback share one device cleanly.

Drop `MIC_BLOCKSIZE` from 3200 (200 ms) to **1280 samples (80 ms)** as part of this refactor. 1280 is openWakeWord's native frame size, so the wake detector consumes one capture frame per inference with no internal reframing. Scribe is unaffected — `stream_audio_to_scribe` does not aggregate chunks, and its VAD / gating constants are time-based, not chunk-based. Side benefits: faster first partial transcript, finer-grained mic-peak meter for barge-in. As part of this refactor, change the capture callback (`respeaker.py`) to log rate-limited on truthy `status` instead of silently dropping the chunk — going deaf without a log is the failure mode we want to avoid at the smaller block size.

`VoiceSession` should stop calling `microphone_chunks()` as a stream opener; it should **subscribe** to the shared capture started by `robot_voice.py`. The subscriber API is an `async def mic_frames(self) -> AsyncIterator[bytes]` per subscriber, backed by an internal bounded queue, so Scribe's existing `async for chunk in audio_chunks` loop is unchanged. Bounded-queue policy differs per subscriber: wake uses a small queue (~10 frames) with silent drop-on-full; Scribe uses the current ~50-frame queue with drop + warn on full. Neither subscriber may block the PortAudio callback.

Stop/cancel paths change meaning: after the refactor, `VoiceSession.stop()` only unsubscribes — it must not close the shared stream. Stream lifecycle is owned by `robot_voice.py` and tied to process exit, not to session start/stop.

Chime-vs-TTS playback: chime PCM goes through the same `_run_playback()` queue as TTS so the XVF3800's AEC sees it on the speaker reference path. Chime is only ever played from the `armed` state, so it never overlaps with assistant speech; document this invariant so a future contributor doesn't add a mid-conversation confirmation chime and break it.

### Phase 0 implementation steps

1. **`ReSpeakerAudio` lifecycle** — add `start_io` / `stop_io`; one `RawInputStream`, one `RawOutputStream`, both opened on `start_io` and torn down on `stop_io`.
2. **Capture fan-out** — single capture task reads 6-ch interleaved PCM, extracts the configured channel, applies gain, publishes mono frames; `mic_frames()` returns a per-subscriber async iterator backed by a bounded queue.
3. **Block size** — drop `MIC_BLOCKSIZE` to 1280 samples (80 ms).
4. **Overflow logging** — capture callback logs rate-limited on truthy `status` instead of silently dropping.
5. **Output stream stays open** — `begin_playback` / `end_playback` (and any new `play_wav`) become queue lifecycle operations; the PortAudio output device opens on `start_io` and closes on `stop_io`.
6. **`VoiceSession` rewire** — `VoiceSession.start()` calls `mic_frames()` (not `microphone_chunks()`); `VoiceSession.stop()` only unsubscribes, does not close streams.
7. **`robot_voice.py` ownership** — call `start_io` on service start, `stop_io` on shutdown; nothing else opens PortAudio.
8. **Unit tests** — fakes for `mic_frames` fan-out (bounded queue + drop policy), output stream staying open across begin/end cycles.

### Phase 0 acceptance

This phase ships **only** when the voice assistant works identically to before. No wake word, no chime — the only goal is "refactor without regression."

On the Raspberry Pi, with `robot-voice` running in today's always-hot mode (`enabled: true`, no wake fields set):

- [ ] Unit tests pass with fakes.
- [ ] `journalctl -u robot-voice` shows the service comes up, opens Scribe, transcribes speech, and produces a spoken TTS reply — same as before the refactor.
- [ ] **Multi-turn conversation works.** At least 3 user turns + 3 assistant replies in one session, no device-busy / reopen errors between turns.
- [ ] **Barge-in still works.** Speaking over the assistant cuts TTS at roughly the same latency as before (the finer-grained mic-peak meter from the smaller blocksize may make it slightly faster; that's fine, but it must not be slower or flaky).
- [ ] **Start/stop cycling is clean.** Toggle `enabled` off → on → off → on at least 5 times via the dashboard or by editing `voice.json`. No ALSA / device-busy errors, no leaked tasks, mic still works after the last cycle.
- [ ] **No sustained input overflows.** Soak the service for 10+ minutes of normal use; rate-limited overflow log lines from the new callback should be rare-to-absent.
- [ ] **Audio levels in the dashboard** still reflect mic activity correctly (`mic_peak`, `playback_rms`, barge-in gate indicators).
- [ ] No new `sounddevice` stream opens outside `ReSpeakerAudio.start_io` (grep check).

Only after all of these pass on real hardware does Phase A begin.

## Custom Model — "Hey Bloop"

Use [openWakeWord](https://github.com/dscripka/openWakeWord) with a custom model trained on the phrase **`hey bloop`** (training configs usually lowercase the target phrase).

Training (offline, not on the Pi):

1. Use the upstream automatic training notebook or `examples/custom_model.yml` with `target_phrase: ["hey bloop"]`.
2. Generate thousands+ positive clips (synthetic TTS is normal for openWakeWord).
3. Include strong negative data (speech, TV, music without the phrase).
4. Export `.onnx` for Pi inference.
5. Store on the robot at e.g. `/home/pi/.config/robot-pet/models/hey_bloop.onnx` and commit a copy under `models/wake/` in the repo if size is reasonable.

For Phase A plumbing, a **temporary** pretrained model (e.g. `hey jarvis`) is acceptable to test capture → score → chime, then swap in `hey_bloop.onnx`.

Tuning on hardware:

- `wake_threshold` in config (start ~0.5, adjust for false accepts vs misses).
- Re-run tests in the robot's real room with TV/music.

## Phase A — Wake Word + Chime Only

### Goal

Prove: **"Hey Bloop" → detection → local chime**, with no cloud voice session.

### Scope

In scope:

- Shared `ReSpeakerAudio` capture/playback refactor.
- Thin `src/voice/wakeword.py` wrapper around openWakeWord (load onnx, `predict(frame)`).
- Local chime asset (short WAV, 16 kHz mono PCM).
- Config fields for model path and threshold.
- Telemetry or logs for detections (count, last score, last fire time).
- Pi manual test procedure.
- Skip the `ELEVENLABS_API_KEY` / `OPENAI_API_KEY` credential check when running in wake-only mode — wake doesn't need either, and today's `has_credentials()` gate would block Phase A from starting on a Pi without keys.

Out of scope for Phase A:

- ElevenLabs Scribe / OpenAI / Flash.
- `VoiceSession` start/stop.
- Idle timeout back to dormant (Phase B).
- Dashboard changes (optional later in A if trivial).

### Suggested shape

Keep one entrypoint; add a wake-only mode rather than a second service. Mode is derived from three `voice.json` fields:

- `enabled: bool` — master switch for the service.
- `wake_word_enabled: bool` — when true, the service runs the wake loop; in Phase A this is the only thing it does, in Phase B it gates `VoiceSession` start on a detection.
- `force_active: bool` (Phase B) — debug bypass: skip wake, start `VoiceSession` immediately (preserves today's always-hot behavior for dashboard testing).

In Phase A, `enabled && wake_word_enabled` runs the wake-only loop; `enabled && !wake_word_enabled` keeps today's always-hot `VoiceSession` path unchanged.

For development, also allow a small diagnostic script that owns audio exclusively while `robot-voice` is stopped:

- `scripts/diagnostics/wakeword-chime-test.py` — loads model, runs shared capture, plays chime on fire.

Use the diagnostic script for first hardware bring-up; use wake-only mode in `robot-voice` as the Phase A ship target.

### Phase A implementation steps

(Assumes Phase 0 has shipped — shared capture, `mic_frames()`, persistent output stream, 80 ms blocks all in place.)

1. **`play_wav` for chime** — add to `ReSpeakerAudio` if not already done in Phase 0; loads 16 kHz mono WAV and pushes PCM through the existing playback queue (same path TTS uses, so AEC sees the chime on the speaker reference).
2. **Dependencies** — add `openwakeword` (+ onnx runtime) to `pyproject.toml`; confirm Pi install in `setup.sh` if needed.
3. **`wakeword.py`** — load model from config path; frame size matches openWakeWord expectations; return score + fired bool.
4. **Wake loop** — async task: for each mono frame from subscriber, run detector; on fire, debounce (e.g. 2–3 s), call `play_wav(chime)`, log/telemetry.
5. **Suppression during chime** — ignore wake scores while chime is playing (avoid double-fire).
6. **Config** — extend `VoiceConfig` / `voice.json`:
   - `wake_word_enabled: bool`
   - `wake_word_model_path: str`
   - `wake_threshold: float`
   - `wake_chime_path: str`
7. **Model artifact** — train `hey_bloop.onnx` or document interim model + swap procedure.
8. **Tests** — unit tests with synthetic PCM and a fake scorer; no hardware in CI.

### Phase A acceptance

On the Raspberry Pi, with `robot-voice` running in wake-only mode (or the diagnostic script, not both):

- [ ] Service starts without opening Scribe or OpenAI sockets.
- [ ] Saying **"Hey Bloop"** reliably triggers detection within ~1 s of finishing the phrase.
- [ ] A short chime plays through the ReSpeaker speaker path.
- [ ] False accepts during normal room conversation are rare enough to keep iterating (exact rate TBD on hardware).
- [ ] `journalctl -u robot-voice` shows wake events with score/threshold.
- [ ] Leaving the process running for several minutes does not lose the mic (no device-busy errors).

## Phase B — Integrate With Voice Session

### Goal

Default experience:

1. Dormant: wake-only loop (Phase A).
2. Wake fires → chime → **start** `VoiceSession` (Scribe + assistant + TTS).
3. User has a multi-turn conversation (existing behavior).
4. Idle timeout or explicit stop → **stop** `VoiceSession`, return to dormant wake loop.

### State machine

```text
disabled
  -> armed (wake listening, no cloud)
  -> active (VoiceSession running)
  -> armed (after idle or stop)
```

`voice.json` direction (same fields introduced in Phase A — no rename):

- `enabled: true` + `wake_word_enabled: true` → `armed` (wake listening, no cloud).
- `force_active: true` → bypass wake, go straight to `active` (current always-hot behavior, dashboard debug).

Do not require two systemd units.

### Phase B implementation steps

1. **Orchestrator in `robot_voice.py`** — owns state `armed | active | disabled`; only one mode uses the mic for cloud streaming.
2. **On wake** — play chime (Phase A path), then `await voice_session.start()`; subscribe Scribe to the **same** mic fan-out.
3. **On idle** — `await voice_session.stop()`; clear or keep conversation history (default: keep for follow-ups within session; clear on return to armed — decide in implementation, document in config).
4. **`session_idle_secs`** — no Scribe commits for N seconds → end session.
5. **Wake suppression** — ignore wake detections while `state == active` (whole session, not just during chime or assistant speech). Mid-conversation "hey bloop" must not start a second session or replay the chime.
6. **Telemetry** — statuses: `waiting` (armed), existing `listening` / `hearing` / `thinking` / `speaking` (active).
7. **Dashboard** — "Listen" becomes armed; optional "Talk now" for `force_active`.
8. **Deprecate always-hot cloud** — `enabled: true` without wake should mean armed, not instant Scribe, unless `force_active`. **This is a behavior break** for anyone currently using the dashboard Listen toggle: today it streams to Scribe immediately, after Phase B it sits silent until "hey bloop." `force_active` exists as the explicit opt-in to the old behavior.

### Phase B acceptance

- [ ] Robot sits silent in the cloud while armed; no Scribe traffic until wake.
- [ ] "Hey Bloop" → chime → user question → spoken reply works end-to-end.
- [ ] Follow-up turn works without saying the wake word again.
- [ ] After idle, robot returns to wake-only; saying "Hey Bloop" starts a new session.
- [ ] Barge-in still works during TTS (existing logic).
- [ ] No ALSA/device-busy errors when transitioning armed ↔ active repeatedly.

## Testing Strategy

| Layer | Phase A | Phase B |
|-------|---------|---------|
| Unit | Fake mic frames → fake detector → `play_wav` called | State transitions; Scribe not started when armed |
| Pi manual | Diagnostic or wake-only service | Full conversation loop |
| Room soak | 30+ min armed, note false accepts | TV on, robot speaks, verify no self-wake storm |

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Device busy / dead mic after reopen | Shared capture refactor first; never open two input streams |
| Wake hears its own chime | Suppress detection during chime + TTS; tune threshold |
| Custom model weak on "Bloop" | More positive clips; negatives with similar words; threshold tuning |
| openWakeWord install heavy on Pi | Pin versions in `pyproject.toml`; test on Pi early in Phase A |
| Phase B regresses barge-in | No change to turn policy in Phase B; only gate when Scribe runs |

## Files (expected)

| File | Phase |
|------|-------|
| `src/drivers/respeaker.py` | A — shared capture/playback |
| `src/voice/wakeword.py` | A |
| `src/robot_voice.py` | A wake-only; B orchestrator |
| `src/config/voice.py` | A config; B idle/force flags |
| `models/wake/hey_bloop.onnx` | A (or documented Pi path) |
| `assets/audio/wake_chime.wav` | A |
| `scripts/diagnostics/wakeword-chime-test.py` | A bring-up |
| `tests/test_wakeword.py` | A |
| `tests/test_robot_voice_wake.py` | B |

## Done Means (Personality Phase 1)

When Phase B acceptance passes, the open items in [personality-phase-1.md](../phases/personality-phase-1.md) for wake word, chime, and idle-to-wake-only are satisfied. Reliability tuning may continue after that without blocking the phase.
