> Sensor positions below are also given in `base_link` (REP-103: +x forward,
> +y left, +z up). The origin is the drive-axle midpoint on the ground centerline;
> the axle sits 55 mm behind the bumper front face. Authoritative mount poses live
> in `src/robot_model.py`.

## INNER CHASSIS

275mm wide

- excluding wheels and power button
  332mm wide
- including wheels and power button

240mm deep

- excludes caster, speaker and wheels
  330mm deep
- including caster, speaker and wheels

Ground clearance
15.5mm to screws
23mm to platform

Steel platform thickness 48.5mm

Platform distance from ground: 71.5mm

---

bumper

- 25mm from frame to inner

---

Downward facing cliff ToF sensors (measured):
67mm off the ground (`z = 0.067`)
128mm off center, left and right (`cliff_left y = +0.128`, `cliff_right y = -0.128`)
angled 35 degrees downward (`pitch = +0.611 rad`; positive pitch is nose-down in a z-up frame)
mounted at the leading edge — top edge sits ~9mm forward of the bumper (`x ≈ +0.064`)
left and right only; there is no center cliff sensor

---

RPLidar A1 (measured 2026-10-25):
rotation centre 82mm behind the drive axle (`x = -0.082`), 135.5mm forward of the chassis rear (axle to chassis rear is 217.5mm)
centred: 135mm from the right chassis side, against a 275mm chassis (`y = 0`)
scan plane 130mm above the platform, 203mm above the floor (`z = 0.203`)
motor and cable point to the rear, so the 0 degree mark faces forward (`yaw = 0`)
the scan plane clears the camera mast

---

## TO MEASURE (CAD model, 2026-10-26)

The CAD model in `~/Documents/CAD Studio/Robot/model.py` uses the values above.
These are still guesses; replace each with a measurement (mm, `base_link`
frame: origin at the axle midpoint on the floor, +x forward, +y left, +z up).
Once measured, move the sensor-relevant ones into `src/robot_model.py` and
rerun `scripts/generate-urdf.py`.

Electronics bay (what sits on the steel platform):
- top of the tallest box/board above the floor (assumed 120)
- footprint on the platform: width, depth and front edge x (assumed 240 wide, 204 deep, front at x = +4)

Wheels and motors:
- true wheel width and outer face y (assumed 26 wide, outer face at y = ±166)
- hub/shaft bore (assumed 8 mm REX) and whether the hub sticks out past the tyre

Bumper:
- bumper bar height: bottom and top above the floor (assumed 45 to 85)
- bar thickness (assumed 7.5) and the arms/brackets that hold it: how many, y position, size
- cliff sensor board size and how far its tip pokes past the bumper face (assumed 14 x 8 x 8, tip at x = +64)

Rear caster:
- ball diameter (assumed 25.4) and ball centre x (assumed -253)
- caster bracket: width, length, top height above the floor (assumed 36 wide, 48.5 long, top at z = 40)

Lidar mount:
- what the A1 sits on (bracket/post) and its footprint (assumed a 50 x 50 pedestal from the bay top to the lidar base)
- lidar base height above the floor (assumed 173 = scan plane 203 minus 30)
- motor housing length behind the scanner body (assumed 25, body diameter 70, body height 40)

Camera mast:
- mast x position, cross-section and top height (assumed x = -20, 20 x 20, top at z = 185)
- camera head size (assumed 36 wide x 20 deep x 24 tall)

Speaker:
- position, size and height above the floor (assumed 50 x 70 x 50 on the rear of the bay, bottom at z = 80, rear face at x = -250)

Other:
- power button position and size (not modelled)
- Bambu X2D bed size (model assumes 350 x 350)
