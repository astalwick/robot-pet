# Plan 3: Time budget, stall recovery, sensor depth, and a model-frame viewer

## Problem

Even with the instrument panel (plan 1) and camera overlay (plan 2), goal navigation
fails in predictable ways:

1. **The clock kills the goal.** `MAX_SECONDS = 120` in `src/voice/agent_runner.py` is
   roughly 10-15 model steps once every step carries an image and medium reasoning. A goal
   like "find the doorway and go through it" dies during its first block-and-recover cycle
   and presents as "the robot gave up at the first obstacle."
2. **The wheel-snag failure is invisible.** When the camera clears a gap but the body
   doesn't, the doorframe slides *beside* the bumper, outside all three forward ToF cones
   (see `docs/forward tof sensor geometry.md` — coverage is only ~±174 mm directly ahead).
   The safety gate never trips; the wheels stall; the move ends as `encoder_no_progress`.
   Today that result gets no recovery hint (`_attach_goal_motion_observation` only hints on
   `safety_blocked`), and `_action_log_line` renders it as a plain "moved 0.10 forward" —
   the model is told a partial move succeeded.
3. **The model has no depth.** The corridor overlay is angular: it answers "does that
   obstacle threaten me *if* it is at 0.5 m / 1.0 m", but the model has no distance
   estimate, so it cannot know which corridor pair applies. Meanwhile the robot owns three
   real forward distance measurements, and they reach the model as raw
   `{"name": "cliff_left", "distance_mm": 72}` dicts — a cliff sensor's normal
   floor reading looks like an obstacle 7 cm away, and nothing says which readings are
   forward clearance versus floor checks.
4. **Nobody can see what the model sees.** The annotated frames exist only inside OpenAI
   API requests. Debugging "why did it think that" means guessing.

## Goal

Goals get enough time to recover from mistakes; a wheel snag comes back as its own error
with a recovery hint; sensor readings arrive interpreted (role, clearance, threshold);
forward depth is repeated next to the camera image AND drawn onto it as a corridor pair at
the measured distance; and the exact frames sent to the model are viewable in the web
dashboard.

Parts A, B, and D are independent. Within Part C, do C1 then C2 first; C3 and C4 build on
C2's interpreted readings.

---

## Part A: A realistic goal time budget

### Changes in `src/voice/agent_runner.py`

- `MAX_SECONDS` (line ~68): `120.0` → `420.0`. `MAX_STEPS = 60` stays as the runaway
  guard — steps, not wall clock, are the real bound on runaway behavior.

### Incidental prompt fix

`config/operational_system_prompt.md` (the "Iterative goals" bullet list) still says
`move` takes "positive seconds forward, negative backward". The tool takes
`distance_meters` now (`MOVE_TOOL` in `src/voice/assistant.py`). Rewrite that bullet to
match: positive meters forward, negative backward.

### Tests

No new tests. `max_seconds` is a keyword argument, so existing runner tests are
unaffected. Run `python3 -m unittest tests.test_voice_agent_runner` to confirm.

---

## Part B: Stalled moves get named and hinted

Context: an encoder-stalled move already completes with
`{"ok": false, "error": "encoder_no_progress", "traveled_m": ...}`
(`src/robot_motion.py` `_check_encoder_move_progress` → `_end_encoder_move`).

### Changes in `src/voice/agent_runner.py`

- `_attach_goal_motion_observation` (~line 280): alongside the existing `safety_blocked`
  hint block, when `enriched.get("error") == "encoder_no_progress"` set:

  > "Your wheels stalled but no front sensor tripped. You are probably snagged on
  > something beside your body at wheel height, like a doorframe edge or a furniture leg.
  > Back up straight to free yourself. Do not turn in place while snagged."

- `_action_log_line` (move branch, ~line 196): treat `encoder_no_progress` like the
  blocked case so the log reads `moved 0.5 forward (stalled at 0.10)` instead of
  `moved 0.10 forward`.
- `motion_camera_caption` (~line 233): the caption currently appends `(blocked)` only for
  `safety_blocked`. Append `(stalled)` for `encoder_no_progress`.

### Tests

Extend `tests/test_voice_agent_runner.py`:
- a move output with `error: "encoder_no_progress"` gains the snag hint after observation
  attachment
- the action log line for that move says "stalled at" with the partial distance
- the camera caption for that move ends with "(stalled)"

Run: `python3 -m unittest tests.test_voice_agent_runner`

---

## Part C: Interpreted sensor readings, forward depth beside and on the image

### C1: Publish role and threshold with each reading (`src/robot_sensors.py`)

Telemetry readings today carry only `name`, `kind`, `channel`, `distance_mm`, `ok`
(`reading_to_dict` in `src/telemetry/messages.py`). Roles and thresholds live in the
sensor service's own `SensorsConfig` — right there at the publish site.

In `SensorsService.tick` (the `readings=[reading_to_dict(reading) for ...]` line, ~123):
look up each reading's `SensorEntry` by name in `self.config.sensors` and merge into the
reading dict:

- `role`: the entry's role (`"forward"`, `"cliff"`, or absent/None)
- for `forward` entries: `stop_below_mm` from `forward_stop_mm(entry, self.config.safety)`
- for `cliff` entries: `trip_above_mm` from `cliff_trip_mm(entry, self.config.safety)`

Both helpers already exist in `config/sensors.py` (the safety gate uses them). A reading
with no matching entry or no role keeps today's shape. Do the merge at the call site with
a small dict built from `self.config.sensors`; leave `reading_to_dict` alone — other
callers don't have a config.

Added keys are backward compatible: the safety gate and dashboards read by key.

### C2: Interpret readings in the surroundings view (`src/voice/assistant.py`)

In `inspect_robot_snapshot` (~line 480), the sensors block currently copies
`name` / `distance_mm` / `ok` per reading. Replace the per-reading dict with an
interpreted one, keyed off the new `role`:

- `role == "forward"` and ok with a distance:
  `{"name", "role": "forward", "clearance_m": <distance_mm/1000, 2 decimals>,
  "stops_below_m": <threshold/1000 or None>, "tripped": <distance_mm < stop_below_mm>}`
- `role == "cliff"` and ok with a distance:
  `{"name", "role": "cliff", "status": "cliff_detected" if distance_mm > trip_above_mm
  else "floor_normal"}` — the raw floor distance is what confused the model; drop it.
- missing role, not-ok, or missing distance/threshold: keep today's shape
  (`name` / `distance_mm` / `ok`), passing `role` through if present.

This intentionally changes `check_surroundings` output for the normal assistant turn too —
same improvement, same code path.

### C3: Forward depth in the goal observation caption (`src/voice/agent_runner.py`)

In `_attach_goal_motion_observation`, the surroundings dict is already fetched before the
camera snapshot. Pull the `role == "forward"` readings out of it and append one sentence
to the caption text part built by `motion_camera_caption`:

> "Forward sensors: left 0.42 meters, center 0.90 meters, right 0.31 meters."

- Map a reading to left/center/right by substring of its `name`.
- A not-ok or missing reading reads "left unavailable".
- If there are no forward-role readings at all, append nothing.
- Compose the sentence in `_attach_goal_motion_observation` (or a small helper next to
  `motion_camera_caption`); do not push it into robot_motion.

The same numbers ride in the tool output JSON via C2 — repeating them in the text adjacent
to the image is deliberate, so the model reads depth and picture together.

### C4: Draw the corridor at the measured distance (`src/voice/camera_overlay.py`)

This is the payoff of the depth work. The fixed 0.5 m / 1.0 m pairs only answer "would I
fit *if* the obstacle were at that distance"; with real depth we draw the pair at the
distance the nearest obstacle actually is, using the same tested angle math.

In `camera_overlay.py`:

- Extend the signature:
  `annotate_snapshot(jpeg_bytes, forward_clearances_m=None)` where the new argument is a
  dict like `{"left": 0.42, "center": 0.90, "right": None}` (`None` = unavailable).
  Default `None` keeps today's behavior exactly.
- When at least one clearance is present, take the **minimum** available value (the
  nearest obstacle is the binding constraint) and draw a third corridor pair at that
  distance with the existing `_corridor_half_angle` + `angle_to_x`, in a distinct color
  (orange, e.g. `(0, 120, 255)` BGR), labeled like `body @0.42m SENSED`.
  Skip drawing the pair when the minimum is under 0.15 m — the half-angle blows up and
  the robot has to back out regardless.
- Draw one small text line at the top-left in the same color:
  `fwd L 0.42  C 0.90  R --  m` (`--` for unavailable), so the numbers travel with the
  picture — including into the Part D viewer.

Wire clearances into all three `annotate_snapshot` call sites:

- Add a helper `forward_clearances(surroundings) -> dict[str, float | None]` in
  `src/voice/assistant.py` next to `check_surroundings_snapshot`: pull `role == "forward"`
  readings from the surroundings dict (C2 shape), map to left/center/right by substring of
  the reading's `name`, `None` when not-ok or absent. Three call sites use it, so the
  helper earns its existence.
- `src/voice/agent_runner.py` `_attach_goal_motion_observation`: the surroundings dict is
  already in hand before the camera snapshot — pass
  `annotate_snapshot(jpeg, forward_clearances(surroundings))`.
- `src/voice/tools.py` `look` (~line 277) and `_scan` (~line 196): fetch a fresh
  surroundings snapshot via `context.robot_inspection_caller` +
  `check_surroundings_snapshot` (the same pattern `check_surroundings` itself uses at
  ~line 295-301), then pass the clearances. In `_scan`, fetch inside the per-image loop so
  the readings match the heading the robot is facing for that shot. If the caller is
  `None` or the fetch fails, pass `None` — the overlay falls back to the fixed pairs.

### Prompt update

Update the Camera sections of `config/agent_system_prompt.md` and
`config/operational_system_prompt.md`: the forward sensor distances tell you how far the
nearest obstacles ahead actually are, and the orange SENSED corridor pair shows your body
width at exactly that distance — if the gap you are aiming at does not fully enclose the
SENSED pair, your body will not fit through it.

### Tests

- `tests/test_robot_sensors.py`: a published forward reading carries `role` and
  `stop_below_mm`; a cliff reading carries `role` and `trip_above_mm`; an entry with no
  role publishes today's shape.
- The existing tests covering `inspect_robot_snapshot` / `check_surroundings_snapshot`
  (grep `tests/` for `check_surroundings_snapshot`): forward reading becomes
  `clearance_m` + `tripped`; cliff reading becomes `status`; unroled reading unchanged.
- `tests/test_voice_agent_runner.py`: the observation caption contains the forward-sensors
  sentence when the fake telemetry includes forward readings, and "unavailable" for a
  failed one.
- `tests/test_camera_overlay.py`: `annotate_snapshot` with clearances still returns valid
  JPEG bytes; with `forward_clearances_m=None` behaves as before; the sensed pair uses the
  minimum clearance (check via `_corridor_half_angle`/`angle_to_x` math the existing tests
  already exercise); a sub-0.15 m minimum skips the sensed pair.
- `forward_clearances`: forward readings map to left/center/right; not-ok reading gives
  `None`; no forward readings gives all-`None`.

Run: `python3 -m unittest tests.test_robot_sensors tests.test_voice_agent_runner
tests.test_camera_overlay` plus the surroundings test module found above.

---

## Part D: See the frames the model sees

### Why not the voice timeline

The timeline republishes its entire buffered event list on every telemetry publish
(`TimelineBuffer.snapshot` in `src/robot_voice.py`, called at ~1 Hz), and events live for
the whole horizon. Base64 JPEGs in timeline events would bloat every publish for minutes.
Images don't belong in the event stream — a filename-sized pointer barely helps either,
because the assistant-turn tool path (`look` in `tools.py`) doesn't emit timeline events.

Instead: the voice process writes every annotated frame to a small disk ring, and the web
dashboard (same Pi) serves and displays them. Timestamped filenames line up with the
timeline by eye.

### New module `src/voice/model_frames.py`

Flat module, constants at top, one entry point:

```python
MODEL_FRAMES_DIR = Path("/tmp/robot-pet-model-frames")
MAX_FRAMES = 40

def save_model_frame(jpeg_bytes: bytes, label: str, caption: str = "") -> None:
    """Save a frame that was sent to the model. Never raises."""
```

- Filename `{int(time.time() * 1000)}-{label}.jpg` where `label` is a short slug like
  `look`, `scan`, `goal-move`. Millisecond names sort lexicographically, which is the
  prune order.
- If `caption` is non-empty, write it to a `.txt` sidecar with the same stem.
- Create the directory (`mkdir(parents=True, exist_ok=True)`), then prune oldest `.jpg`
  files (and their sidecars) beyond `MAX_FRAMES`.
- Wrap the whole body in `try/except OSError` with a `log.warning` — a full or read-only
  disk must never break a tool call. That is the module's only defensive branch.

Reference `MODEL_FRAMES_DIR` inside the function (not captured at import) so tests can
patch it.

### Call sites (all three places `annotate_snapshot` runs)

- `src/voice/tools.py` `look` (~line 277): `save_model_frame(jpeg, "look")`
- `src/voice/tools.py` `_scan` (~line 196): `save_model_frame(jpeg, "scan")`
- `src/voice/agent_runner.py` `_attach_goal_motion_observation`:
  `save_model_frame(jpeg, f"goal-{call.name}", caption)` — pass the caption string it
  already builds, save after `annotate_snapshot` so the file is byte-identical to what the
  model receives.

### Dashboard routes (`src/robot_web_dashboard.py`)

The dashboard is an aiohttp app on the same host; import `MODEL_FRAMES_DIR` from
`voice.model_frames` (it already imports from `voice.personality`).

- `GET /api/model-frames`: list the directory, newest first, capped at `MAX_FRAMES`:
  `{"frames": [{"name": "...jpg", "t": <mtime>, "caption": "<sidecar text or empty>"}]}`.
  Missing directory → empty list.
- `GET /model-frames/{name}`: serve the file as `image/jpeg`. Validate `name` against
  `^[0-9]+-[a-z0-9_-]+\.jpg$` and 404 anything else — that regex is the path-traversal
  guard; never join un-validated input into the path.

### Dashboard UI (`src/web_dashboard_static/`)

A "Model frames" panel in the voice area of `index.html`, following the existing
panel/JS patterns (plain JS modules, no framework):

- Poll `/api/model-frames` every 2 seconds while the page is visible.
- Render the newest ~8 as thumbnails (`img src="/model-frames/{name}"`), caption on
  hover via `title`, click opens the image in a new tab.
- Skip re-rendering when the newest filename hasn't changed.

### Tests

- New `tests/test_model_frames.py`
  (`python3 -m unittest tests.test_model_frames`):
  - saving writes a jpg (and txt when captioned) into a patched temp dir
  - saving beyond `MAX_FRAMES` prunes the oldest files and their sidecars
  - an unwritable dir (patch `MODEL_FRAMES_DIR` to a path under a file) does not raise
- Extend `tests/test_robot_web_dashboard.py`: list endpoint returns newest-first with
  captions; fetch serves bytes; `../etc/passwd` and `foo.txt` style names get 404.

---

## Out of scope

- Ground-plane corridor projection (drawing the corridor as converging floor lines).
  It needs the camera's mount height and pitch measured and calibrated, and a
  slightly-wrong pitch draws confident floor lines in the wrong place — worse than none.
  C4's sensed-distance pair answers the same "do I fit at the distance the obstacle
  actually is" question with the already-verified linear angle mapping. If the model
  still misjudges fit after this plan, ground-plane projection is the next plan.
- Timeline event ↔ frame correlation in the UI; timestamps are enough for now.
- Overlays or frame capture on the dashboard MJPEG stream; this touches only frames sent
  to the model.
- Any change to turn safety gating or doorway-approach prompting.

## Style constraints (from CLAUDE.md)

Minimal and flat. Part A is a constant and a prompt line. Part B is added branches at
three existing sites. Part C is added keys at existing call sites, one optional parameter
on `annotate_snapshot`, and two small helpers (`forward_clearances`, the caption
sentence). Part D's only new file pair is `model_frames.py` and its test — one function,
no class, no config. If anything seems to need a manager or a registry, stop and
reconsider.
