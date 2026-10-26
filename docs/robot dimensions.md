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
  335mm from the bumper face to the caster wheel's rearmost point, wheel swung fully back
- (caster rearmost at x = -280; 2026-10-26, replaces the earlier 330; the speaker on the bumper face adds its own protrusion, assumed 12)

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

## CAD MODEL MEASUREMENTS (2026-10-26)

Measured for the CAD model in `~/Documents/CAD Studio/Robot/model.py`
(mm, `base_link`: origin at the axle midpoint on the floor, +x forward, +y left, +z up).

Bumper:
- bar 272 wide, 30 to 65 off the ground, front face at x = +55 (thickness assumed 7.5)
- held by two round posts, 10 thick, centred 42.5 from each bumper end (y = +/-93.5)
- cliff ToF sensors sit on top of the bumper and reach 6 past the bumper face (x = +61)
- speaker is an oval 38 wide x 25 tall, centred on the bumper face (protrusion assumed 12)

Wheels:
- tyres 24 wide, hub does not protrude (outer faces at y = +/-165 with the 306 track)

Rear caster:
- swivel wheel caster, wheel diameter about 48
- wheel swung fully back, bumper face to the wheel's rearmost point is about 335 (x = -280, 62.5 behind the platform rear)

Camera mast:
- centred on the chassis, about 20 behind the platform front (x = +2.5), top at 180 off the ground
- camera mount 28 wide x 30 tall on a round post 10 thick

Lidar: RPLidar A1M8 datasheet, 98.5 x 70 x 60 overall; scan plane 203 as measured above. It sits on a
3D-printed cover that will be replaced (model uses a 50 x 50 stand-in, lidar base at 167).

Still assumed in the CAD model (measure when convenient):
- electronics on the platform (Pi, RoboClaw, battery): height of the tallest item above the floor and its footprint (model uses one 240 x 204 box, top at 120, front edge at x = +8; fine for now)
- camera mount depth (model uses 12)
- caster bracket: how the fork is held behind the platform (model uses a bracket on the rear face, pivot 16 ahead of the axle, wheel 20 wide)
- power button position and size (not modelled)

Printer: Bambu X2D, bed 256 x 256.
