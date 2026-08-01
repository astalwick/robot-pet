# Plan 2: Annotated camera frames for the goal runner

## Problem

The agent must convert "the door is somewhat left of center in a 102-degree-wide image"
into a turn angle, and must judge whether its 332 mm body clears an obstacle it can only
see. Today both are described in prose (`config/operational_system_prompt.md` Camera and
Movement sections) and the model does the geometry mentally. VLMs are poor at FOV
trigonometry from prose but good at reading a labeled ruler drawn on the image. The camera
geometry is fixed and known, so the code can draw the answers directly onto every frame.

## Goal

Every camera frame sent to the model (from `look`, `scan`, and the auto-observation added
by plan 1) carries three overlays:

1. a **center crosshair** (vertical line at image center),
2. a **degree ruler** along the horizontal axis so "center that object" becomes reading a
   number, and
3. a **drive corridor** — vertical line pairs marking the robot's angular width at 0.5 m
   and 1.0 m ahead, so "will my wheel clear that" becomes "does the obstacle sit inside
   the marked corridor".

## Where the code goes

New module `src/voice/camera_overlay.py` with one entry point:

```python
def annotate_snapshot(jpeg_bytes: bytes) -> bytes:
    """Draw the navigation overlay on a camera JPEG and return new JPEG bytes."""
```

Called in `src/voice/tools.py` at the two places a JPEG becomes a data URL — `look`
(~line 272) and `_scan` (~line 192) — and at the plan-1 auto-observation site in
`src/voice/agent_runner.py`. If plan 1 is not yet merged, wire `look` and `scan` only.

Use OpenCV (`cv2`) — it is already a project dependency (`robot_vision` uses it for face
detection) and is installed in the same environment on the Pi. Decode with
`cv2.imdecode`, draw, re-encode with `cv2.imencode('.jpg', ...)`.

If decoding fails, return the original bytes unchanged — a camera frame without overlay
still beats no frame. That is the only defensive branch this module needs.

## Geometry

### Angle-to-pixel mapping

Keep the mapping in one small pure function so it is testable and reusable by both the
ruler and the corridor:

```python
def angle_to_x(theta_degrees: float, image_width: int) -> int
```

Camera: Raspberry Pi Camera 3 Wide, horizontal FOV 102 degrees, so half-FOV
`HALF_FOV_H_DEGREES = 51.0`. Use the linear (equidistant) approximation:

```
x = width / 2 + (theta / 51.0) * (width / 2)
```

with **positive theta = left of center mapping to smaller x** (image left). This matches
the `turn` tool's sign convention (positive degrees = turn left) so a label can translate
directly into a turn argument.

The wide lens has barrel distortion, so this is approximate — a few degrees off near the
edges is fine for this purpose. Do not add lens calibration or undistortion in this plan.

### Degree ruler

- Tick marks every 10 degrees from -50 to +50 along a horizontal line near the bottom of
  the frame (bottom is usually floor, which is less likely to occlude anything the model
  needs).
- Label every 20 degrees as `L20`, `L40`, `R20`, `R40` (left/right words avoid sign
  confusion entirely). Center tick unlabeled — the crosshair marks it.
- Crosshair: a full-height vertical line at center, visually distinct from the ticks.

### Drive corridor

Robot half-width including wheels is 166 mm (`docs/robot dimensions.md`: 332 mm overall).
An obstacle at distance D dead ahead threatens the body if it lies within
`±atan(0.166 / D)` of center:

- at **0.5 m**: ±18.4 degrees
- at **1.0 m**: ±9.4 degrees

Draw each pair as partial-height vertical lines (say, the lower half of the frame) at
`angle_to_x(±18.4)` and `angle_to_x(±9.4)`, in two distinguishable colors, with small
labels like `body @0.5m` and `body @1m` near the bottom of each line.

This deliberately avoids projecting a ground-plane trapezoid, which would require camera
mounting height and pitch calibration. The angular-corridor version needs only the FOV
constant. A ground-plane version can be a later upgrade if this proves useful.

### Drawing style

High-contrast but thin: 1-2 px lines, small font, semi-transparent where cv2 makes that
easy (drawing on a copy and `cv2.addWeighted` is acceptable; don't overbuild). The overlay
must not hide the scene — the model still has to see the obstacles.

## Teach the model to read it

The overlay is useless if the model doesn't know what the lines mean. Update, keeping each
to a sentence or two:

- `LOOK_TOOL` and `SCAN_TOOL` descriptions in `src/voice/assistant.py` (~lines 259-315):
  mention that images carry a degree ruler (L/R labels give the turn angle: an object at
  L20 needs `turn` with `degrees=20`; at R20, `degrees=-20`) and corridor lines showing
  the robot's own width at half a meter and one meter.
- `config/agent_system_prompt.md`: replace the current wide-angle-estimation guidance
  (paragraph starting "Your camera is a wide-angle Pi Camera 3...") with: read the target's
  ruler position and turn by that number; before driving, check nothing intrudes inside
  the corridor lines closer than the matching distance.
- `config/operational_system_prompt.md`: same replacement for the "Camera" section and the
  FOV-estimation sentences in "Movement and Orientation". The wheelbase warning paragraph
  can shrink to a pointer at the corridor lines.

## Tests

New `tests/test_camera_overlay.py` (`python3 -m unittest tests.test_camera_overlay`):

- `angle_to_x`: center maps to width/2, L51 maps to 0, R51 maps to width, L>R ordering
  correct (positive angle → left half of image).
- `annotate_snapshot` on a small synthetic JPEG returns valid JPEG bytes of the same
  dimensions (decode the result and check shape).
- `annotate_snapshot` on garbage bytes returns the input unchanged.

No pixel-perfect assertions on the drawing itself.

## Out of scope

- Lens undistortion / calibration.
- Ground-plane corridor projection (needs camera height + pitch measurement).
- Overlays on the dashboard MJPEG stream — this touches only the frames sent to the model
  in the voice process; `robot_camera` and the dashboards are unchanged.

## Style constraints (from CLAUDE.md)

One flat module, two functions, constants at the top. No config file for colors or tick
spacing — hardcode them. No class.
