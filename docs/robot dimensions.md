> Sensor positions below are also given in `base_link` (REP-103: +x forward,
> +y left, +z up). The origin is the drive-axle midpoint on the ground centerline;
> the axle sits 51 mm behind the bumper front face, 24 mm behind the frame front and
> 216 mm ahead of the frame rear (measured 2026-10-26, replaces 55 and 217.5).
> Authoritative mount poses live in `src/robot_model.py`.

## INNER CHASSIS

272mm wide (silver frame, outside to outside, measured beside the wheels 2026-10-26, replaces 275)

- excluding wheels and power button
  272mm wide
- including wheels and power button
  332mm wide: 272 frame + 6 gap + 24 tyre on each side (2026-10-26); a tape across the wheels read 330, about 2 short; the power switch stays inside the wheels

240mm deep (silver frame, outside to outside, confirmed 2026-10-26)

- excludes caster, speaker and wheels
  335mm from the bumper face to the caster wheel's rearmost point, wheel swung fully back
- (caster rearmost at x = -284; 2026-10-26, replaces the earlier 330; the speaker is flush with the bumper face)

Ground clearance
15.5mm to screws
24.5mm to the platform underside (measured 2026-10-26, replaces 23)

Steel platform thickness 48.5mm

All heights "above the platform" are from the top of the silver frame. A black perforated plate 3 thick lies on top of the frame and stays inside its outline. The frame's corners are square.

Platform distance from ground: 73.0mm to the platform top (24.5 underside + 48.5 thickness; 2026-10-26, replaces 71.5)

---

bumper

- front face 27mm ahead of the frame front; the bar is 2.5mm thick, so 24.5mm from frame to inner (measured 2026-10-26)
- the bumper is bent a little, so expect a millimetre or two either way

---

Forward ToF boards: Adafruit VL53L1X 3967, 25.5 wide x 17.5 tall per Adafruit, 4 thick with the sensor chip (measured), standing upright (measured 2026-10-26):
- the two side boards stand on round posts 6.7 thick; their top edges are 60 above the platform top (133 off the floor), so the board centres, where the chips sit, are about 124 off the floor (`z = 0.124`)
- the side posts are mirrored: each post centre is 24 in from its side of the frame (y = ±112) and 88 behind the frame front (x = -64), in line with the IMU post on the left
- each side board is 2 thick and centred on its post; the sensor chip adds 2, so the chip face is 3 ahead of the post centre (x = -61); the two connectors stick out about 1 past the chip
- the centre board is clipped to the camera post below the camera; its top edge is 46 above the platform top (119 off the floor), so its centre is about 110 off the floor (`z = 0.110`)
- the back of the centre board is 16.5 behind the frame front (x = +7.5), so its chip face is 12.5 behind the frame front (x = +11.5)

Downward facing cliff ToF sensors (measured):
Blue VL53L0X boards with a white JST plug, 20 x 20, 9 thick including the plug (measured 2026-10-26; not the Adafruit 3317 the doc used to guess; the size matches Seeed's Grove boards)
67mm off the ground (`z = 0.067`)
128mm off center, left and right (`cliff_left y = +0.128`, `cliff_right y = -0.128`)
angled 35 degrees downward (`pitch = +0.611 rad`; positive pitch is nose-down in a z-up frame)
mounted at the leading edge — top edge sits ~9mm forward of the bumper (`x ≈ +0.060`)
left and right only; there is no center cliff sensor

---

RPLidar A1 (measured 2026-10-25):
rotation centre 82mm behind the drive axle (`x = -0.082`), 135.5mm forward of the chassis rear (with the axle 216mm from the chassis rear, these two disagree by 1.5mm; the printed cover flexes and shifts, so this is as close as it can be measured without taking the mic off; we use 82)
centred side to side (`y = 0`, confirmed 2026-10-26)
the lidar is 55mm tall and sits on a printed stand whose top is 150mm above the floor, so its top is 205mm above the floor (checked twice); the laser sits about 9mm below the top, so the scan plane is about 196mm above the floor (`z = 0.196`) and about 123mm above the platform (2026-10-26, replaces 203)
motor and cable point to the rear, so the 0 degree mark faces forward (`yaw = 0`)
the scan plane clears the camera mast

---

## CAD MODEL MEASUREMENTS (2026-10-26)

Measured for the CAD model in `~/Documents/CAD Studio/Robot/model.py`
(mm, `base_link`: origin at the axle midpoint on the floor, +x forward, +y left, +z up).

Bumper:
- bar 272 wide, 30 to 65 off the ground, 2.5 thick, front face at x = +51 (27 ahead of the frame front)
- held by two round posts, 10 thick, centred 42.5 from each bumper end (y = +/-93.5)
- cliff ToF sensors sit on top of the bumper and reach 9 past the bumper face (x = +60)
- speaker is an oval 38 wide x 25 tall, centred on and flush with the bumper face

Wheels:
- tyre outside diameter 95.7 (measured 2026-10-26; the odometry's 100.2 is calibrated from distance travelled, not the tyre size)
- tyres 24 wide, hub does not protrude, 6 gap between each tyre and the frame (measured 2026-10-26)
- so the tyres run from y = +/-142 to +/-166, with wheel centres 308 apart

Rear caster:
- swivel wheel caster, wheel diameter 50 (measured 2026-10-26, replaces about 48)
- wheel swung fully back, bumper face to the wheel's rearmost point is about 335 (x = -284, 68 behind the platform rear)
- swivel pivot 24 behind the platform rear and centred side to side (x = -240, y = 0; measured 2026-10-26), so the wheel's axle trails about 19 behind it
- hangs from an L-shaped metal bracket on the frame's rear face: 32 wide, 2.5 thick, its flat arm on top and level with the frame top (73 off the floor, underside at 70.5); its leg reaches 40 down the rear face (bottom at 33)
- caster wheel 20 wide (measured)

Camera mast:
- round post 12 thick, centred on the chassis, its centre 24 behind the frame front, so directly above the drive axle (x = 0); top of the camera at 180 off the ground
- camera mount 28 wide x 30 tall; front to back it is the 3 swivel mount plus the 3.5 the camera sticks out, so 6.5 deep
- the post ends in a tilting swivel mount 3 thick, centred on the post; the camera sticks out 3.5 past it, so with the camera level the lens tip is 5 ahead of the post centre (x = +5)
- the camera tilts, so the cover needs room for it to swing
- the centre forward ToF board is clipped to the same post below the camera, 1.5 in front of the post

Mic array (measured 2026-10-26, not modelled yet):
- ReSpeaker 4-mic circular board in a 3D-printed round holder, 78 across and 12.5 tall
- holder sits on a round shaft 12 thick that rises from the caster's swivel, so it shares the pivot point (x = -240, y = 0)
- top of the holder 147.5 off the floor, bottom at 135

Inside the frame (not on the deck): the RoboClaw motor controller, the battery and the drive motors.

Cables: nothing hangs past the frame's sides or rear. The only cables outside the frame are the cliff sensor
cables, which run from the front of the frame across to the bumper at its two ends.

Raspberry Pi 5 in a case, sitting on a Waveshare board (measured 2026-10-26, not modelled yet):
- case 91 front to back and 60 side to side (the long side runs front to back)
- top of the Pi 34 above the platform top (107 off the floor), including the Waveshare board under it
- wires and the camera cable on top reach another 32, so 66 above the platform top (139 off the floor), lightly pressed down
- back edge 73 forward of the platform rear (x = -143)
- right edge, at its switch that sticks out a little, 110 in from the platform's right edge (y = -26)

Voice LED indicator (custom printed box, shows listening, thinking and speaking; measured 2026-10-26, not modelled yet):
- box 22.5 side to side x 40.5 front to back, top about 26 above the platform top (99 off the floor)
- back edge 74 forward of the platform rear (x = -142)
- left edge 66 in from the platform's left edge (y = +70 to +47.5)
- wires come out of its top into the Pi's pins and reach the same 66 above the platform top

Power switch (rocker switch zip-tied to the outside of the rear-left corner; measured 2026-10-26, not modelled yet):
- sticks out 26 past the platform's left side (out to y = +162, just inside the left wheel's outer face at +166)
- sticks out about 9 past the platform rear (x = -225)
- with its wire connectors, reaches 72 forward of the platform rear along the left side (x = -144)
- runs the full height of the frame, 24.5 to 73 off the floor (the zip tie holds it a little crooked)

IMU (Adafruit BNO085 board 4754, 25.6 x 22.7 x 4.6 per Adafruit, standing upright on a post; measured 2026-10-26, not modelled yet):
- centre 88.2 forward of the platform rear (x = -127.8)
- centre 24 in from the platform's left edge (y = +112)
- top of the board 62.5 above the platform top, 135.5 off the floor, so the board centre is about 124 up (`z = 0.124`)

Lidar: RPLidar A1M8, 98.5 x 70 footprint per the datasheet, 55 tall measured; top at 205 and scan plane at 196, as measured above. It sits on a
3D-printed stand, top at 150, that will be replaced (model uses a 50 x 50 stand-in).

Lidar stand on today's cover (measured 2026-10-26; the CAD model still uses the stand-in):
- the lidar sits on a black plate held up by four hex posts, 5 across the flats, that go into the cover's flat top
- the posts make a trapezoid, 55 apart side to side at the front, 40 apart at the back, and 70 front to back (centre to centre)
- the front posts are about 7 behind the front edge of the lidar's round top (x ≈ -54), so the back posts are at x ≈ -124; hard to measure, so allow a few mm

Still assumed in the CAD model (measure when convenient):
- electronics on the platform: now measured (Pi, voice LED box, IMU above; the RoboClaw and battery are inside the frame), but the model still uses one 240 x 204 box, top at 120

Printer: Bambu X2D, bed 256 x 256.
