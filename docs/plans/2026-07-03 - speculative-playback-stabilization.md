# Speculative Playback Stabilization

Goal: make the speculative turn path stable against mid-utterance pauses — the
robot must not talk over a user who paused to think, and when a collision does
happen anyway, recovery must be instant and the user's full sentence must be
preserved.

Style rules: this repo is allergic to enterprise code. Read `CLAUDE.md` before
starting and follow it. Prefer deleting code over adding it. If a step here
seems to require breaking a CLAUDE.md rule, stop and ask.

Run tests with `python3 -m unittest tests.test_voice_core` (plus the other
suites named per stage below).

## Background — read this before touching code

How a turn flows today (all in `src/voice/assistant.py` unless noted):

1. Scribe partials arrive. A stable partial schedules `start_after_stable_partial`
   (line ~1576), which starts a **speculative** turn: the LLM and TTS begin
   working immediately, but the audio is held behind `ActiveTurn.playback_event`.
2. When Scribe's VAD decides the utterance ended (~1.0s of silence), a commit
   arrives. `handle_commit` (~1669) matches it against the speculative prompt and
   calls `ActiveTurn.confirm()` (~634), which opens playback **immediately**.
   This is the path that makes speculation fast: LLM+TTS latency is hidden
   inside the VAD silence window.
3. Separately, if `policy.speculative_playback_enabled` is true, `start_turn`
   (~1570) also creates `release_speculative_playback` (~1279): sleep 0.8s, then
   open playback as soon as the mic RMS has been quiet for 0.65s — **without
   waiting for any commit**.

Key fact discovered during analysis: `speculative_playback_enabled` gates
**only** step 3. Speculation itself (steps 1–2) runs regardless of the flag.
The flag is currently off (`src/config/voice.py:42`) because the robot was
interrupting users on thinking pauses.

Why step 3 is the problem — timeline math with the user stopping speech at t=0:

- Speculative turn starts at ~t=0.65s (0.35s partial delay + 0.65s local quiet).
- The RMS release fires at earliest ~t=1.45s (0.8s sleep, quiet already satisfied).
- The commit confirm fires at ~t=1.1–1.3s (1.0s VAD threshold + network).

So when the user is genuinely done, the commit wins the race anyway. The RMS
release only fires first when the commit is *late* — which is exactly when
Scribe's neural VAD believes the user is not done (filler sounds, breath,
thinking pause). The RMS release contributes almost zero latency and nearly all
of the interruptions. We delete it.

The second failure mode is unavoidable by any release logic: the user pauses
longer than the VAD threshold, Scribe commits (it cannot read minds), the robot
starts speaking, and the user resumes. Today that continuation is routed into
barge-in (`handle_partial` ~1624), where the user must beat an RMS ≥700 gate
sustained 350ms — fighting their own robot. Worse, the resumed speech is a
fresh Scribe utterance, so the restarted turn sees only the fragment ("...and
the tomatoes") and the first half of the sentence is lost. We fix recovery
instead: instant retraction plus utterance stitching.

## Fixed Decisions

- Speculative playback opens **only** on commit confirm. The pre-commit RMS
  release path is deleted, along with the `speculative_playback_enabled` flag
  (its only effect was that path).
- A speculative turn whose commit never arrives is cancelled quietly by a
  watchdog; it never speaks and never enters history.
- A partial that arrives within a short grace window after playback opens is
  treated as "the user was not done": stop playback immediately (no barge-in
  RMS gate, no cooldown), and restart speculation with the stitched utterance
  (previous commit text + new fragment).
- The half-spoken robot response from a retracted turn stays in history
  (existing cancel semantics: what was audible is recorded). Accepted trade-off.
- Partials ending in an obvious continuation word ("and", "but", "the", ...)
  are treated as incomplete and do not start speculation.
- Scribe's `vad_silence_threshold_secs` becomes a voice config field
  (default 1.0, unchanged). Tuning it up is now safe because response latency
  no longer includes LLM+TTS.
- False starts (robot audibly spoke, then was retracted) get a session counter
  published through the status callback, as the tuning KPI.
- Out of scope — do not touch: barge-in policy for non-continuation speech,
  echo suppression, Scribe socket lifecycle, wake word, TTS chunking, the goal
  runner.

## Existing Code Shape

Line numbers are anchors as of this writing; re-check with grep before editing.

- `src/voice/assistant.py`
  - `ActiveTurn` (~588–656): `confirm()` ~634 opens playback and cancels
    `playback_release_task`; `request_cancel()` ~643 also cancels it.
  - `cancel_active_turn(reason)` ~1257: emits `turn_cancel` (with
    `was_speaking`), records audible speech into history, stops playback.
  - `release_speculative_playback` ~1279 (**delete**),
    `release_committed_playback` ~1292 (keep — it serves non-speculative turns).
  - `start_turn` ~1509: creates the turn and the release task (flag branch
    ~1570–1574).
  - `start_after_stable_partial` ~1576: the debounce that starts speculation.
  - `handle_partial` ~1603: dedupe → echo check → playback/barge-in branch
    (~1624–1641, returns) → speculative prompt-match branch (~1645–1652, which
    includes the `looks_incomplete_partial` release-cancel hook ~1649) →
    commit-continuation branch (~1654) → debounce restart (~1666).
  - `handle_commit` ~1669: end-session → echo → `commit_decision` → goal cancel
    → playback mismatch barge-in → non-speculative pre-playback match →
    speculative confirm (~1742–1755) → else cancel + restart non-speculative.
  - `TurnRuntimeState` (~693): per-session mutable state (`active_turn`,
    `debounce_task`, `last_local_speech_at`, ...).
- `src/voice/turn_policy.py`: `TurnPolicy` frozen dataclass; `speculation_decision`
  ~78; `looks_incomplete_partial` ~65; `turn_policy_from_config` ~227.
- `src/config/voice.py`: `VoiceConfig` field ~42, `from_values` ~78,
  `VOICE_FIELDS` dashboard schema entry ~238.
- `src/voice/elevenlabs_io.py`: `SCRIBE_VAD_SILENCE_THRESHOLD_SECS = 1.0`
  (line ~28), used in the Scribe URL params (~119).
- `src/voice/session.py`: `VoiceSession.start()` constructs the
  `scribe_streamer(...)` call — where a config value gets plumbed in.
- `tests/test_voice_core.py`: the big behavior suite. Directly affected:
  `test_spoken_speculative_turn_without_commit_reaches_history` (~3432, built
  on the RMS release — its asserted behavior is being removed) and
  `test_speculative_turn_waits_for_commit_when_playback_is_disabled` (~3490,
  which becomes the unconditional behavior). Grep the whole tests/ tree for
  `speculative_playback_enabled` and `speculative_playback_delay_secs`.

## Stage 1 — Delete the pre-commit RMS release; add a no-commit watchdog

The core change. After this stage, a speculative turn can only start speaking
because a commit confirmed it.

1. Delete `release_speculative_playback` (assistant.py ~1279–1290). Keep
   `release_committed_playback`.
2. In `start_turn` (~1570), replace the flag-gated branch. Speculative turns
   get a watchdog instead of a release task:

   ```python
   if speculative:
       turn.playback_release_task = asyncio.create_task(cancel_unconfirmed_speculation(turn))
   else:
       turn.playback_release_task = asyncio.create_task(release_committed_playback(turn))
   ```

   Reusing the `playback_release_task` slot means `confirm()` and
   `request_cancel()` clean the watchdog up with zero new plumbing.
3. Add the watchdog next to the release functions:

   ```python
   async def cancel_unconfirmed_speculation(turn: ActiveTurn) -> None:
       await asyncio.sleep(policy.speculative_no_commit_timeout_secs)
       if state.active_turn is turn and turn.speculative and not turn.playback_event.is_set():
           # Clear our own handle first so cancel_active_turn does not cancel
           # the task it is running inside.
           turn.playback_release_task = None
           await cancel_active_turn("no_commit")
           status(status="listening", assistant_working=False)
   ```

   Add `speculative_no_commit_timeout_secs: float = 8.0` to `TurnPolicy`.
4. Remove the now-dead pieces:
   - `TurnPolicy.speculative_playback_delay_secs` and the
     `SPECULATIVE_PLAYBACK_DELAY_SECS` constant (only consumer was the deleted
     function — verify with grep).
   - `TurnPolicy.speculative_playback_enabled` and its wiring in
     `turn_policy_from_config`.
   - `VoiceConfig.speculative_playback_enabled`: the field, its `from_values`
     line, and its `VOICE_FIELDS` entry. Saved config files that still contain
     the key are fine — `from_values` reads keys explicitly and ignores
     extras; confirm that with a quick test before relying on it.
   - The release-cancel hook in `handle_partial` (~1649–1651): the branch that
     cancels `playback_release_task` when a partial "looks incomplete". It only
     made sense for the RMS release; the watchdog must keep running while the
     utterance continues. The speculative prompt-match branch becomes: replace
     the prompt if `should_replace_speculative_prompt`, otherwise just return.
5. Tests (`tests.test_voice_core`, `tests.test_voice_config`):
   - Rewrite `test_spoken_speculative_turn_without_commit_reaches_history` to
     assert the new contract: with no commit, the turn never opens playback and
     the watchdog cancels it — history stays empty. Use a small
     `speculative_no_commit_timeout_secs` (e.g. 0.05) in the test policy.
   - `test_speculative_turn_waits_for_commit_when_playback_is_disabled`: this
     is now the unconditional behavior. Rename it (drop "when_playback_is_disabled")
     and remove the flag from its policy.
   - Add: commit matching the speculative prompt opens playback (confirm path)
     — this may already exist; verify.
   - Fix all constructions of `TurnPolicy`/`VoiceConfig` that pass the removed
     fields.

## Stage 2 — Continuation grace window: instant retraction + utterance stitching

Handles the unavoidable case: VAD committed on a pause, robot started talking,
user resumed. Requires Stage 1.

1. `ActiveTurn`: add `playback_opened_at: float | None = None`, set inside
   `open_playback()`:

   ```python
   def open_playback(self) -> None:
       if not self.playback_event.is_set():
           self.playback_opened_at = asyncio.get_running_loop().time()
       self.playback_event.set()
   ```

   (All call sites run inside the event loop.)
2. `TurnPolicy`: add `continuation_grace_secs: float = 1.5` and
   `continuation_min_words: int = 2`.
3. `TurnRuntimeState`: add `utterance_prefix: str = ""` and
   `utterance_prefix_deadline: float = 0.0`.
4. In `handle_partial`, inside the `if playback:` branch (~1625), **before**
   `consider_playback_barge_in`, add the retraction check. It applies only to
   the active turn (not goal `ProgressSpeaker` narration):

   ```python
   if (
       playback is state.active_turn
       and playback.playback_opened_at is not None
       and now - playback.playback_opened_at <= policy.continuation_grace_secs
       and len(re.findall(r"\S+", text)) >= policy.continuation_min_words
   ):
       first_half = playback.committed_text or playback.prompt
       state.utterance_prefix = f"{state.utterance_prefix} {first_half}".strip()
       state.utterance_prefix_deadline = now + 10.0
       emit("false_start", turn_id=playback.turn_id, text=text)
       await cancel_active_turn("continuation_retraction")
       status(status="hearing", partial_transcript=text)
       await cancel_task(state.debounce_task)
       state.debounce_task = asyncio.create_task(start_after_stable_partial(f"{state.utterance_prefix} {text}"))
       return
   ```

   Notes for the implementer:
   - `cancel_active_turn` already stops the speaker and records the audible
     fragment into history — do not add extra stop-playback calls.
   - The echo check at ~1616 already ran, so a partial that is actually the
     robot hearing itself never reaches this point.
   - The word-count floor stops a one-word noise transcription from killing
     playback.
5. Stitching for the rest of the utterance. While a prefix is pending, every
   prompt formed from partials/commits must include it, because Scribe only
   sends the fragment:
   - In `handle_partial`, immediately after the `if playback:` branch (i.e. at
     the point where the turn is not speaking), apply the prefix before any
     matching or debounce logic:

     ```python
     if state.utterance_prefix and now < state.utterance_prefix_deadline:
         text = f"{state.utterance_prefix} {text}"
     elif state.utterance_prefix:
         state.utterance_prefix = ""
     ```

     Everything downstream (the speculative prompt-match at ~1645, the debounce
     restart at ~1666) then naturally sees the stitched text, and it matches
     the stitched speculative turn started in step 4.
   - In `handle_commit`, after the end-session and echo checks but before
     `commit_decision`, apply the same stitching and then clear the prefix
     (`state.utterance_prefix = ""`). The commit consumes it.
   - Also clear the prefix on explicit interrupts and end-session, so a stale
     first-half never leaks into an unrelated turn.
6. Tests (`tests.test_voice_core`):
   - Partial arriving within the grace window stops playback (assert
     `stop_playback_now` fired / `turn_cancel` reason `continuation_retraction`)
     and the next started turn's prompt is `"<first half> <fragment>"`.
   - Partial arriving *after* the grace window goes through the normal barge-in
     decision (existing behavior preserved).
   - A one-word partial inside the grace window does not retract.
   - A commit arriving while a prefix is pending produces a stitched turn
     prompt and clears the prefix.
   - Goal narration (`ProgressSpeaker`) playback is unaffected by the grace
     window.

## Stage 3 — Trailing-continuation words block speculation

Small policy-only change in `src/voice/turn_policy.py`. Mid-sentence pauses
usually end on function words; six unpunctuated words (`enough_words`) is
exactly what such a pause looks like.

1. Add to `TurnPolicy`:

   ```python
   incomplete_trailing_words: frozenset[str] = field(
       default_factory=lambda: frozenset(
           {"and", "but", "or", "so", "because", "then", "if", "the", "a", "an", "of", "to", "with", "for"}
       )
   )
   ```
2. Extend `looks_incomplete_partial`: terminal punctuation still wins (so
   "what is that for?" stays complete), otherwise a trailing continuation word
   marks the partial incomplete:

   ```python
   def looks_incomplete_partial(self, text: str) -> bool:
       stripped = text.strip()
       if stripped.endswith(self.incomplete_partial_suffixes):
           return True
       if stripped.endswith(self.complete_partial_suffixes):
           return False
       words = self.normalized_transcript(text).split()
       return bool(words) and words[-1] in self.incomplete_trailing_words
   ```
3. Effect: `speculation_decision` returns `incomplete_partial` for these, so
   speculation simply starts a bit later (on the commit). Nothing else consumes
   `looks_incomplete_partial` after Stage 1 removed the release-cancel hook —
   verify with grep.
4. Tests (`tests.test_voice_core` or wherever `TurnPolicy` unit tests live):
   "turn left and" → incomplete; "I want to know about the" → incomplete;
   "what is that for?" → complete; "bring me the ball please" → complete.

## Stage 4 — Make Scribe's VAD silence threshold a config field

With release commit-gated, total response time ≈ VAD threshold + ~0.3s. The
threshold is now the *single* latency/patience knob, so it must be tunable
without a code deploy.

1. `src/config/voice.py`: add `vad_silence_threshold_secs: float = 1.0` to
   `VoiceConfig`, parse it in `from_values`, and add a `VOICE_FIELDS` entry
   (copy the shape of an existing float field like `barge_in_cooldown_secs`;
   a sensible dashboard range is 0.5–2.0).
2. `src/voice/elevenlabs_io.py`: give `stream_audio_to_scribe` a
   `vad_silence_threshold_secs` parameter defaulting to
   `SCRIBE_VAD_SILENCE_THRESHOLD_SECS`, and use it in the URL params (~119).
3. `src/voice/session.py`: pass `self.config.vad_silence_threshold_secs` in the
   `scribe_streamer(...)` call in `VoiceSession.start()`.
4. Tests: `tests.test_voice_config` for the new field;
   `tests.test_elevenlabs_io` if it asserts Scribe URL params;
   `tests.test_voice_session` constructions.
5. Do not change the default in this project. Tuning guidance for later, on the
   robot: once Stages 1–2 feel stable, try 1.2 — more pause room than today
   with total latency still below the old non-speculative path.

## Stage 5 — False-start counter

The tuning KPI: how often did the robot audibly speak and then get retracted?

1. In `cancel_active_turn`, when `reason == "continuation_retraction"` and the
   turn was speaking (the existing `turn.is_speaking()` read at the
   `turn_cancel` emit — capture it before `turn.cancel()` clears events),
   increment a `false_starts: int = 0` counter on `TurnRuntimeState` and
   publish it: `status(false_starts=state.false_starts)`.
2. The `false_start` timeline event was already added in Stage 2; nothing more
   needed there. Verify the status key flows through `robot_voice.py`'s status
   publishing untouched (status callbacks merge dicts; adding a key should be
   free — confirm, don't assume).
3. Test: a retraction increments the counter and the status callback saw it.

## Validation

- Full test run: `python3 -m unittest tests.test_voice_core tests.test_voice_config tests.test_elevenlabs_io tests.test_voice_session` and then the whole suite.
- On-robot smoke checklist:
  1. Normal question → robot answers; first audio lands roughly at
     VAD-threshold + ~0.3s after you stop talking.
  2. Mid-sentence pause shorter than the VAD threshold → no interruption,
     answer addresses the full sentence.
  3. Pause *longer* than the VAD threshold, then keep talking → robot may start
     a syllable, but cuts off immediately without you raising your voice, and
     the eventual answer addresses the full stitched sentence.
  4. Say "stop" during robot speech → explicit interrupt still works.
  5. Start a goal ("go find the ball") → narration and barge-in on narration
     behave as before.
  6. Watch `false_starts` in the status stream across a few conversations —
     this is the number to tune `continuation_grace_secs` and the VAD threshold
     against.
