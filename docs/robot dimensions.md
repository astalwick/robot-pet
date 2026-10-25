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
