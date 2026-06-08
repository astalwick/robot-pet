## Forward VL53L1X sensor geometry

Use three forward-facing VL53L1X sensors for near-front obstacle detection.

Coordinate system:

- `x` is side-to-side position from the robot centerline.
- Negative `x` is left, positive `x` is right.
- `S` is sensor inset behind the front bumper.
- `B` is obstacle clearance in front of the bumper.
- `D` is distance from the sensor to the obstacle plane.

Relationship between bumper clearance and sensor distance:

- `D = B + S`
- `B = D - S`

The VL53L1X field of view is treated as a 27 degree cone, or +/- 13.5 degrees.

- `tan(13.5 degrees) = 0.240`
- One straight-ahead sensor covers `x +/- 0.240D` at distance `D`.

### Mounting

Left sensor:

- `x = -110 mm`
- `S = 110 mm` behind the front bumper
- height = `120 mm` off the ground
- yaw = `0 degrees`, facing straight forward

Center sensor:

- `x = 0 mm`
- `S = 35 mm` behind the front bumper
- height can be lower than the side sensors
- yaw = `0 degrees`, facing straight forward

Right sensor:

- `x = +110 mm`
- `S = 110 mm` behind the front bumper
- height = `120 mm` off the ground
- yaw = `0 degrees`, facing straight forward

### Horizontal coverage

Using the full robot width of 332 mm, the robot extends about +/- 166 mm from center.

At bumper clearance `B`:

- left sensor covers `-110 +/- 0.240(B + 110)`
- center sensor covers `0 +/- 0.240(B + 35)`
- right sensor covers `+110 +/- 0.240(B + 110)`

Continuous no-gap coverage across the full 332 mm width starts at about `157 mm` in front of the bumper.

At `B = 157 mm`:

- left/right sensor distance is `267 mm`
- center sensor distance is `192 mm`
- left sensor covers about `-174 mm` to `-46 mm`
- center sensor covers about `-46 mm` to `+46 mm`
- right sensor covers about `+46 mm` to `+174 mm`

The outer robot width is covered earlier, at about `123 mm` in front of the bumper, but there are still small gaps between the center cone and side cones.

Practical stop thresholds:

- left/right sensors: `230-240 mm`
- center sensor: `155-165 mm`

Those thresholds correspond to about `120-130 mm` clearance in front of the bumper.

At that range, walls should be detected cleanly. Chair legs around 2 inches wide should also be detected even though the cones are not mathematically continuous. Bumper switches should still be treated as the final hard-stop backup.

### Vertical clearance

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
