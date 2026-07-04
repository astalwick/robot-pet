# Plan 1: Instrument-panel tool results for the goal runner

## Problem

During goal-oriented navigation, the LLM agent gets almost no proprioceptive data:

- A safety-blocked `move` returns `{"ok": false, "error": "safety_blocked"}` — it does not say
  which sensor blocked, how close the obstacle is, or how far the robot actually traveled.
- A completed `turn` returns `{"ok": true, "result": "completed"}` — no measured rotation.
- After moving, the model must *choose* to spend extra steps on `look` / `check_surroundings`
  and mentally correlate three separate results. It often doesn't.
- Nothing tracks pose. The system prompt asks the model to mentally accumulate its moves
  ("if your cumulative moves total 180, the doorway is behind you"), which LLMs are bad at.

Meanwhile the motion service already measures everything we need: encoder travel
(`EncoderMove` in `src/robot_motion.py`), IMU-closed-loop turn rotation
(`turned_degrees` in `src/control/motion_intent.py`), and a safety reason that names the
tripped sensor (`src/control/safety_gate.py:44-49`, e.g. `front_right_obstacle`). All of it
is discarded before the model sees it.

## Goal

Every motion tool result in the goal runner becomes a small instrument panel: what was
commanded, what was measured, what the sensors say now, what the camera sees now, and where
the robot believes it is relative to the goal start. Blocked results say which side is
blocked and include a recovery hint.

This plan has three parts. Implement them in order; each builds on the last.

---

## Part A: Enrich motion intent completion payloads (robot_motion + motion_intent)

The intent completion payload is a plain JSON dict sent back over the intent Unix socket
(`complete({...})` call sites in `src/robot_motion.py`, protocol in
`src/control/motion_intent.py:694` `request_motion_intent`). Adding keys is backward
compatible — the voice layer serializes whatever dict it gets.

### Changes in `src/control/motion_intent.py`

- Add an optional `details: dict | None = None` field to `IntentTick`.
- When a `turn` or `face_me` intent finishes (any result: `completed`, `turn_stalled`,
  `imu_unavailable`), populate `details` with the measured rotation, e.g.
  `{"measured_degrees": <signed float>}`. The accumulator is `self._active.turned_degrees`
  (signed sum of per-tick yaw deltas). **Verify the sign convention** against `_yaw_delta`
  and report it in the same convention as the tool argument (positive = left); flip if
  needed. Capture the value *before* clearing `self._active`.

### Changes in `src/robot_motion.py`

- `_tick_intent` (~line 703): when `tick.finished`, merge `tick.details` into the completion
  dict for both the success and failure branches. Failure example:
  `{"ok": false, "error": "turn_stalled", "measured_degrees": 12.5}`.
- Encoder moves: `_encoder_move_should_stop` / `_complete_encoder_move` / `_end_encoder_move`
  (~lines 726-802). The travel is `self.encoder_move.last_travel / ENCODER_COUNTS_PER_METER`
  (with sign from the commanded direction). Include it on every move completion:
  - completed: `{"ok": true, "result": "completed", "traveled_m": 0.52}`
  - safety block: `{"ok": false, "error": "safety_blocked", "traveled_m": 0.31,
    "blocked_by": <self._last_safety_reason>}` — the reason string already names the sensor
    and kind, e.g. `front_right_obstacle` or `left_cliff`. Pass it through verbatim.
  - `encoder_no_progress`: include `traveled_m` too.
  - Note `_end_encoder_move`/`_fail_pending_intent` currently take only a reason string;
    thread a details dict through (smallest change wins — an optional parameter is fine,
    a new abstraction is not).
- A move blocked before the encoder snapshot exists (`safety.blocked` on the first loop)
  has `traveled_m: 0.0`.

### Tests

Extend `tests/test_motion_intent.py` and `tests/test_robot_motion.py`:
- turn completion carries `measured_degrees` with the correct sign for a left and a right turn
- safety-blocked encoder move carries `traveled_m` and `blocked_by`
- completed encoder move carries `traveled_m`

Run: `python3 -m unittest tests.test_motion_intent tests.test_robot_motion`

---

## Part B: Auto-attach an observation after every motion tool (goal runner only)

Scope this to the goal runner (`src/voice/agent_runner.py`), not the shared dispatcher in
`src/voice/tools.py` — the normal assistant turn stays fast and chatty. The runner already
holds `camera_snapshot_caller` and `robot_inspection_caller` as arguments.

### Changes in `src/voice/agent_runner.py`

After `dispatch_tool` returns for a **motion tool** (`move`, `turn`, `face_me` — not
`express`, not `scan` which already returns images, not `stop`):

1. Fetch the telemetry snapshot via `robot_inspection_caller` and build the sensors/vision
   view with `check_surroundings_snapshot` (already importable from `voice.assistant`).
   Merge it into the tool output dict under a `surroundings` key so it rides inside the
   existing `function_call_output` JSON.
2. If the result was `safety_blocked`, append a `hint` key composed in the voice layer
   (keep prose out of robot_motion). Derive the side from `blocked_by`:
   - right sensor → "You are blocked on the right side. Back up 0.2 to 0.3 meters to create
     clearance before turning; turning in place will sweep your right corner into the obstacle."
   - left → mirrored text.
   - center → "Blocked straight ahead. Back up, then pick a direction with more clearance."
   - a `_cliff` reason → "A cliff sensor tripped. Back away from the edge before anything else."
   - Match on substrings of the reason (`left`/`right`/`center`/`cliff`); if the reason is
     unrecognized, pass it through without a hint.
3. Fetch a camera snapshot via `camera_snapshot_caller` and append it as an image message
   (same `input_text` + `input_image` data-URL shape as `look` in `src/voice/tools.py:276`),
   captioned with the motion that produced it plus the pose line from Part C, e.g.
   "Camera view after moving forward 0.31 meters (blocked). You are 1.4 meters forward and
   0.3 meters left of your starting point, facing 85 degrees left of your starting heading."
4. Camera or telemetry failures must not fail the motion result — degrade to the plain
   result (the motion really happened). A try/except around each fetch is expected here;
   these are real external failure boundaries.

### Prompt trim

Once observations are automatic, cut the paragraphs in
`config/operational_system_prompt.md` and `config/agent_system_prompt.md` that beg the
model to check after every move ("EVERY SINGLE MOVE should ALWAYS be followed by a camera
and sensor check", the equivalent line in the agent prompt). Replace with one sentence:
"After each move or turn you automatically receive fresh sensor readings and a camera view;
use them before choosing your next action."

### Tests

Extend `tests/test_voice_agent_runner.py`: with fake callers, a `move` step's next model
input contains the merged `surroundings`, the `hint` on a blocked result, and an appended
image message. A failing camera caller still yields the plain motion result.

---

## Part C: Per-goal pose tracking in the goal runner

### Changes in `src/voice/agent_runner.py`

Keep a tiny dead-reckoned pose per goal, starting at (0, 0, heading 0) when the goal
starts. Update it from the **measured** values now present in tool results:

- `turn` / `face_me`: `heading += measured_degrees` (fall back to the commanded degrees
  only if the measured value is absent).
- `move`: `x += traveled_m * cos(heading)`, `y += traveled_m * sin(heading)` — use
  `traveled_m` so blocked moves integrate the partial distance.
- `scan` returns to its starting heading (`src/voice/tools.py:214-220`), so no pose change
  on success.

Also keep a short action log: the last 5 motion results as one-line strings, e.g.
`"turned 30 left (measured 28.4)"`, `"moved 0.5 forward (blocked at 0.31 by front_right)"`.

Prepend a pose block as an `input_text` line to each step's model input (either as part of
the observation caption from Part B or a separate user text part):

> "Position: 1.4 meters forward and 0.3 meters left of where you started this goal, facing
> 85 degrees left of your starting heading. Recent actions: turned 30 left (measured 28),
> moved 0.5 forward (blocked at 0.31, right side)."

Use plain speakable words (left/right/forward), heading normalized to ±180, distances to
two decimals. This is model input, not speech, but the project voice style keeps it prose.

A plain dict or small dataclass local to `agent_runner.py` is enough. No new module, no
class hierarchy — this is ~30 lines including formatting.

### Prompt trim

Remove the "reason about your cumulative moves" paragraph from
`config/operational_system_prompt.md` (the "if your cumulative moves have been 180, the
doorway is behind you" passage) and replace with one line telling the model to trust the
position report it receives each step.

### Tests

Extend `tests/test_voice_agent_runner.py`: after a scripted turn-then-move sequence with
known measured values, the pose text in the next model input matches the expected position
and heading (including a blocked partial move).

---

## Out of scope

- Camera image overlays (separate plan: `plan-2-camera-overlay.md`).
- Raising the goal runner's `MAX_SECONDS` — worth doing but a one-line judgment call for
  the owner, not this plan.
- Any change to the assistant-turn (non-goal) tool path.

## Style constraints (from CLAUDE.md)

Minimal, flat, no new abstractions or manager classes. The pose tracker is local state in
the runner, the hint composer is a small function, and the payload enrichment is added keys
at existing call sites. If a change seems to need a new class, stop and reconsider.
