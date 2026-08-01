# Dashboard Local Path History Plan

Goal: add a dashboard visualization that shows the robot's recent estimated
local path as a 180-second breadcrumb trail. This is a debugging and intuition
tool, not a map. It combines IMU yaw with encoder-derived wheel motion using
dead reckoning.

Follow `AGENTS.md`: keep the implementation small, direct, and dashboard-local
until the data proves useful. Do not introduce a mapping stack, pose graph,
database, or generic visualization framework.

## Desired UX

Add a button in the `IMU Orientation` panel, similar in spirit to the voice
timeline maximize control.

Default dashboard:

- `IMU Orientation` continues to show yaw, pitch, and roll.
- Add a compact `Path` or `Maximize Path` button.
- The path canvas is not active as a large permanent panel.

Maximized view:

- Full-screen/fixed overlay or maximized panel.
- Top-down canvas with:
  - current robot pose as a small triangle/arrow
  - breadcrumb points or a thin trail line
  - points fading by age
  - a simple grid
  - a `Reset Path` button
  - a `Close` or `Minimize` button
- Keep 180 seconds of history.
- Individual points expire by timestamp, not by fixed array length.

The panel should be labeled as estimated/local, for example:

```text
Estimated Local Path
```

Avoid wording like "map" until external correction exists.

## Data Model

Each browser-side path point:

```json
{
  "time": 1781395200.0,
  "x_m": 0.42,
  "y_m": -0.08,
  "yaw_degrees": 91.0
}
```

Keep the rolling history in browser memory only. On dashboard refresh, the path
starts empty. This keeps the first version simple and avoids storing noisy
dead-reckoned paths as durable truth.

## Telemetry Needed

Already available:

- `snapshot.sensors.imu.yaw_degrees`

Still needed:

- encoder-derived wheel movement since the previous motion telemetry sample, or
- absolute left/right encoder positions plus enough metadata to compute deltas.

Preferred telemetry addition:

```json
"odometry": {
  "left_distance_m": 1.23,
  "right_distance_m": 1.21
}
```

These can be absolute distances since service start. The dashboard can compute
deltas between snapshots. Absolute counters are easier to recover from missed
frames than precomputed per-frame deltas.

If `robot-motion` already has raw encoder counts available when the encoder
distance work lands, it can publish calibrated meters there. The dashboard
should not know wheel diameter or encoder counts per revolution.

## Dead-Reckoning Math

For each fresh telemetry snapshot:

1. Read current yaw from IMU.
2. Read current left/right wheel distances.
3. Compute deltas from the previous wheel distances:

   ```text
   left_delta = left_distance_m - previous_left_distance_m
   right_delta = right_distance_m - previous_right_distance_m
   travel_delta = (left_delta + right_delta) / 2
   ```

4. Convert yaw to radians.
5. Integrate:

   ```text
   x += travel_delta * cos(yaw)
   y += travel_delta * sin(yaw)
   ```

6. Append a point with `time`, `x`, `y`, and `yaw_degrees`.
7. Drop every point older than 180 seconds:

   ```text
   point.time >= now - 180
   ```

Turning in place should mostly update the robot triangle heading without adding
travel distance, assuming left/right wheel deltas cancel out.

## Accuracy Expectations

This will drift. That is acceptable for this feature.

Expected to work well:

- recent local trail
- spotting whether turns look roughly right
- spotting encoder sign mistakes
- debugging "did the robot arc or drive straight?"
- short-window behavior review

Expected limitations:

- wheel slip creates position error
- carpet and uneven floors affect distance
- IMU yaw can drift in `game` mode
- pushing or dragging the robot may not match commanded motion
- no loop closure, no obstacle map, no global reference

Keep the UI honest: "estimated local path", not "map".

## Phase 1 - Publish Wheel Distance Telemetry

### Why

The dashboard should not compute meters from raw encoder counts. `robot-motion`
owns motor hardware, wheel geometry, and RoboClaw reads.

### Work

In `robot-motion`, once encoder position reads are available:

1. Convert left/right encoder positions to cumulative wheel distances in meters.
2. Publish them in motion telemetry.
3. Use signed values so reverse motion moves backward.
4. Preserve existing wheel telemetry fields.
5. Do not reset encoders just for the dashboard path.

Suggested payload:

```json
"odometry": {
  "left_distance_m": 0.0,
  "right_distance_m": 0.0
}
```

### Tests

- Unit test meter conversion from counts.
- Unit test telemetry includes both left and right cumulative distances.
- Unit test missing encoder read omits odometry or marks it unavailable without
  breaking existing wheel telemetry.

### Acceptance

```bash
python3 -m unittest tests.test_motor tests.test_robot_motion tests.test_telemetry_messages
```

## Phase 2 - Browser-Side Path State

### Why

The first path view only needs short-lived local history. Browser memory is
enough and avoids adding storage or a backend path service.

### Work

Add a small `path-history.js` module under `src/web_dashboard_static/`.

Keep module state:

- `xMeters`
- `yMeters`
- previous left/right distances
- `points`
- paused/minimized state if needed

Expose simple functions:

- `updatePathHistory(snapshot)`
- `renderPathCanvas()`
- `resetPathHistory()`

No classes are needed unless canvas lifecycle state becomes hard to follow.

When odometry or IMU yaw is missing:

- do not append a point
- keep previous history visible
- show a muted status such as `awaiting odometry`

### Tests

If dashboard JS tests are still only static/server tests, add lightweight
coverage by checking:

- the module is served
- `telemetry.js` imports/calls it
- the HTML includes the canvas and reset button

Do not add a browser test harness just for this phase.

## Phase 3 - IMU Panel Button And Maximized Canvas

### Why

The path view is useful when debugging motion but should not permanently consume
dashboard space.

### Work

In `index.html`:

- Add a button to `IMU Orientation`.
- Add a hidden/maximizable path section with:
  - canvas
  - reset button
  - close/minimize button
  - status text

In CSS:

- Reuse the voice timeline maximized pattern where practical.
- Keep the canvas full-width in the maximized panel.
- Use stable dimensions so opening/closing does not disturb the rest of the
  dashboard.

In JS:

- Toggle maximized path panel from the IMU section button.
- Draw only when visible, or at a modest interval while visible.
- Continue collecting points for 180 seconds even when hidden, as long as
  telemetry is live.

### Canvas Rendering

Draw:

- grid lines
- breadcrumb points fading by age
- optional thin line connecting points
- robot triangle at latest point
- scale text such as `1 m`

Autoscale the view to recent points with padding, with a minimum visible range
so tiny movements do not make the view jump aggressively.

## Phase 4 - Reset And Status Behavior

### Work

`Reset Path` should:

- clear points
- set current pose back to `(0, 0)`
- clear previous encoder distance baseline until the next valid telemetry frame

Status text should report one of:

- `live`
- `awaiting imu`
- `awaiting odometry`
- `stale telemetry`

If telemetry goes stale:

- keep the trail visible
- stop appending new points
- mark status stale

## Future Extensions

Do not implement these in the first pass:

- persistent path history
- map coordinates
- loop closure
- correction from lidar/vision
- obstacle overlays
- multiple saved routes
- backend odometry service

Useful later additions:

- show commanded motion segments
- show turn targets vs actual yaw
- overlay safety stops
- overlay ToF/cliff events at the estimated pose
- export the last 180 seconds as JSON for debugging
