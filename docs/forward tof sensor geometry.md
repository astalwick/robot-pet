## Forward VL53L1X sensor geometry

Use three forward-facing VL53L1X sensors for near-front obstacle detection.

> Positions are in `base_link` (REP-103: +x forward, +y left, +z up, meters).
> The origin is the drive-axle midpoint on the ground centerline. The drive axle
> sits 55 mm behind the bumper front face, so the bumper front is at `x = +0.055 m`.
> Authoritative mount poses live in `src/robot_model.py` (`SENSOR_MOUNTS`).

Coordinate system:

- `y` is side-to-side position from the centerline. **+y is left, -y is right.**
- `S` is sensor inset behind the front bumper. A sensor inset `S` is at
  `x = 0.055 - S` (a larger inset means a smaller, more negative x).
- `B` is obstacle clearance in front of the bumper.
- `D` is distance from the sensor to the obstacle plane.

Relationship between bumper clearance and sensor distance:

- `D = B + S`
- `B = D - S`

The VL53L1X field of view is treated as a 27 degree cone, or +/- 13.5 degrees.

- `tan(13.5 degrees) = 0.240`
- One straight-ahead sensor covers `y +/- 0.240D` at distance `D`.

### Mounting (measured)

Left sensor (`forward_left`):

- `y = +110 mm` (left of center)
- `S = 118 mm` behind the front bumper  →  `x = -63 mm`
- height `z = 126 mm`
- yaw = `0 degrees`, facing straight forward

Center sensor (`forward_center`):

- `y = 0 mm`
- `S = 45 mm` behind the front bumper  →  `x = +10 mm`
- height `z = 110 mm` (lower than the side sensors)
- yaw = `0 degrees`, facing straight forward

Right sensor (`forward_right`):

- `y = -110 mm` (right of center)
- `S = 118 mm` behind the front bumper  →  `x = -63 mm`
- height `z = 126 mm`
- yaw = `0 degrees`, facing straight forward

### Horizontal coverage

> This coverage analysis was worked out with the earlier nominal mounting
> (110 mm inset, 120 mm height) and is kept for its qualitative conclusions. Signs
> follow REP-103 (+y left). The authoritative mount poses are the measured values
> above.

Using the full robot width of 332 mm, the robot extends about +/- 166 mm from center.

At bumper clearance `B`:

- left sensor covers `+110 +/- 0.240(B + 110)`
- center sensor covers `0 +/- 0.240(B + 35)`
- right sensor covers `-110 +/- 0.240(B + 110)`

Continuous no-gap coverage across the full 332 mm width starts at about `157 mm` in front of the bumper.

At `B = 157 mm`:

- left/right sensor distance is `267 mm`
- center sensor distance is `192 mm`
- left sensor covers about `+46 mm` to `+174 mm`
- center sensor covers about `-46 mm` to `+46 mm`
- right sensor covers about `-174 mm` to `-46 mm`

The outer robot width is covered earlier, at about `123 mm` in front of the bumper, but there are still small gaps between the center cone and side cones.

Practical stop thresholds:

- left/right sensors: `230-240 mm`
- center sensor: `155-165 mm`

Those thresholds correspond to about `120-130 mm` clearance in front of the bumper.

At that range, walls should be detected cleanly. Chair legs around 2 inches wide should also be detected even though the cones are not mathematically continuous. Bumper switches should still be treated as the final hard-stop backup.

### Vertical clearance

> Nominal-mounting analysis (side sensors at 120 mm height, 110 mm inset). The
> measured mounting (126 mm height, 118 mm inset) clears the bumper by a little
> more, so the conclusion below is conservative.

The side sensors sit `110 mm` behind the bumper. At that inset, the cone spreads vertically by:

- `110 * tan(13.5 degrees) = 26.4 mm`

The bumper top is `85 mm` off the ground.

With side sensor apertures at `120 mm` off the ground, the lower edge of the cone at the bumper plane is:

- `120 - 26.4 = 93.6 mm`

So the side sensors clear the top of the bumper by about `8.6 mm` when mounted level.

For a level side sensor at 120 mm height, the lower edge of the cone is:

- about `64 mm` high at `125 mm` in front of the bumper
- about `56 mm` high at `157 mm` in front of the bumper

That is fine for walls and chair legs. Very low obstacles may still be missed by the ToF sensors, which is why the bumper switches matter.
