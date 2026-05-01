# Robot Pet — Shopping List & Decisions

Living doc. Edit freely.

---

## Phase 0 — Bench robot ("brain on a desk")

The minimum to start writing code and spinning wheels in the air.

MARKED WITH x, available at abra electronics
MARKET WITH g, available gobilda (intl shipping)
MARKED WITH a, available on amazon (url included)
MARKED WITH r, pishop.ca
MARKED WITH w. walmart
MARKET WITH s, staples
MARKED WITH R, robotshop
Marked with S, seeed studio

- means ordered

### Brain & accessories (~$205)

- [w+] Raspberry Pi 5, 4GB — ~$120
- [r+] Official Active Cooler — ~$5 _(required, not optional — Pi 5 thermally throttles without it)_
- [r+] Pi 5 M.2 HAT — ~$15 _(primary storage — NVMe, not SD)_
- [ ] M.2 2230/2242 NVMe drive, 256GB — ~$35 _(dramatically more reliable than SD; 5–10× faster boot, service starts, and logging)_
- [s+] Lexar 64GB 633X CL10 Micro SDXC— ~$40 _(initial setup + offline backup; primary OS lives on NVMe)_

### Motion / chassis — goBILDA build (~$360)

**Target footprint: 288×240mm (11.3″×9.5″), 2WD differential drive + ball caster.**

Note: side rail plus gusset offset (240mm + 20mm \* 2) = 280mm.

- Structural parts
  - [x+] 2× goBILDA 1120-0009-0240 U-channel (side rails, motors mount here) — ~$13 each ($26)
  - [x+] 2× goBILDA 1121-0009-0240 U-channel (cross-rails) — ~$12 each
  - [x+] 2x goBILDA 1126-0090-0001 Steel Gusset-Plate (3 x 3 Hole) - 4 Pack - 5.99 each
- Wheel / drivetrain parts
  - [x+] 2× goBILDA 5203-2402-0019 Yellow Jacket motor (312 RPM, 19.2:1 ratio, built-in encoder) — ~$40 each ($80)
  - [g+] 2× goBILDA 3626-0014-0096 96mm Hogback Traction Wheel $9.99 each
  - [ ] 2× goBILDA 1401-0043-0036 (OPTIONAL!) hub-mount motor mount bracket — ~8.99 each
  - [x+] 2x goBILDA 1309-0016-4008 1309 Series Sonic Hub (8mm REX® Bore) - 7.99
  - [g+] 2x goBILDA 3801-0919-0300 Encoder Breakout Cable (Female Jumper Wires)
  - [g+] RoboClaw 2x7A motor controller — ~$70 _(reads encoders natively, runs velocity PID onboard, overcurrent/thermal protection. Pi commands "go 0.5 m/s" over serial; controller handles the rest. Cleaner SLAM odometry and a huge software simplification vs. a dumb H-bridge. 7A is ample margin on 5203 motors drawing ~1–3A typical, ~9A at absolute stall.)_
- Caster parts
  - [g+] 1× goBILDA 3621-0001-0001 ball caster (48mm ball) — $7.99
    - https://ca.robotshop.com/products/2-swivel-caster-wheel?qd=bb9a0c18537f90f5776244f9028affed
  - [x+] 1x goBILDA 2814-0016-0008 M8 x 1.25mm to goBILDA Pattern Mount - $5.99
  - [x+] 1x goBILDA 1141-0001-0001 Round-End Steel L-Bracket - 2 Pack - 5.99
- Fasteners
  - [x+] 2x goBILDA 2812-0004-0007 M4 x 0.7mm Nylock Nut - 25 pack - 2.99 each
  - [x+] 2x goBILDA 2800-0004-0010 M4 Screw (10mm) - 25 pack - 3.39 each
  - [x+] 1x goBILDA 2800-0004-0012 M4 Screw (12mm) - 25 pack - 3.39 each
  - [x+] 1x goBILDA 2800-0004-0016 M4 Screw (16mm) - 25 pack
  - [x+] 1x goBILDA 2801-0004-0008 2801 Series Zinc Plated Steel Washer (4mm ID x 8mm OD) - 25 pack

https://cad.onshape.com/documents/db4fc3ddecaa1562d76c8ec8/w/723d49fe6bcf38613c12e8f1/e/6953b2a23ad6b97e0ed668ae

### Power on-robot (~$55)

- [a+] 10,000mAh USB-C PD power bank — ~$25 _(runs the Pi on a separate rail)_
- [a+] Small 3S LiPo battery pack — ~$20–30 _(runs RoboClaw + motors; separate rail)_
  - CNHL 2200mAh 3S Lipo Battery 40C 11.1V Shorty lipo Battery with T Plug for RC Airplane Car Truggy Truck Crawler(2 Packs)
  - https://www.amazon.ca/CNHL-2200mAh-Battery-Airplane-Crawler/dp/B0FF4J6ZW6/ref=sr_1_5_sspa?crid=2C3B6EHYD04Y7&dib=eyJ2IjoiMSJ9.a-1qzTP30RxxR_CZCL3ELOfBRg67pAEGt-PxzBnYPfiUfzZ78ek7c8wAPqwHsfBvCG4knxxYEHbmTldi8rBEVluqAWJX7_9dnQdp7GUhXJSqyKQ2s50KIEhyopD2Cpa1K1KwjFMvyL86CIrq1q2zke8d1sMGkONcdRvu_d5WIvwRu820ZBFN4QzVBnVAfkx7anx8XpmVuvddneO29i8rribES5AaKOE_jMsWLM3UAboB09l4wDzd6YaQQp8rUPra456hQOaJX4wPR_J-qN_bUv9ZENuQk3NyZpZVLtSQPdY.v25bVDqR66AfNyiqpgbiSnvVayM-8X6Ny2UO1voW4Lc&dib_tag=se&keywords=2200mAh+3S+Lipo+Battery+shorty+11.1v&qid=1776560761&sprefix=2200mah+3s+lipo+battery+shorty+11+1v%2Caps%2C130&sr=8-5-spons&sp_csd=d2lkZ2V0TmFtZT1zcF9hdGY&psc=1
- [a+] 3S LiPo battery charger, balanced
  - LiPo Battery Charger 2S-3S RC Balance Charger Compact Charger for 7.4-11.1V LiPo Batteries (Black)
  - https://www.amazon.ca/Battery-Charger-Balance-7-4-11-1V-Batteries/dp/B08HN5DZ5Y/ref=sr_1_13?crid=1QGC3WBKUHVQ7&dib=eyJ2IjoiMSJ9.qfRWbLv08fP7oyXDUW-EVEhlANB6AuasezOrtPFHGYPCUP0tkege0_xe1M1lj8YjLMx2yZh_frAq87l-oxqboCbVqRuXCYpI8a4qcF5clzeZFd5v1cwePF5odl3ICpDfUE87Qmv-7wmZAsThW7vi0OV-8MTPSoC_Lj9iDqchMhvUt84s2GinZ_kEYP4xaE1EpDDD9LRPEH3YgByHVcuIDxxbzYty1Ubcx-crvg3hQZs6e_c8PiXCPpyjX65oVeXVed4lPTXmJV8LPgocnx7DuEmgAnj_aeDMaMMyt_vd2xc.2NeZBDaz7wrs1isq9I85At8DXDeznMjg-7-5JsjXCDk&dib_tag=se&keywords=3s%2Blipo%2Bbattery%2Bcharger%2Bxt60&qid=1776560207&sprefix=3s%2Blipo%2Bbattery%2Bcharger%2Bxt60%2Caps%2C150&sr=8-13&th=1
- [x+] Deans to bare wire
  - For connecting battery to roboclaw
  - Strip off the banana plugs https://abra-electronics.com/interconnects/connectors/power-connector-housing/xt-connectors/cab-t-b-20cm-deans-t-plug-male-to-4mm-banana-plug-charging-lead-rc-cable.html
- [x+] Inline fuse holder
  - battery + -> fuse holder -> switch -> RoboClaw + (positive lead)
- [ ] 20a fuse — ~$5 _(hardwired on motor battery positive)_
  - get a 20a mini blade fuse at canadian tire
  - https://www.canadiantire.ca/en/pdp/littelfuse-20a-maxi-fuse-0201504p.html?rq=20a+fuse
- [x+] Dupont female to female jumper wires
  - https://abra-electronics.com/wire-cable-accessories/wirecable/jumper-wire-assembly/3141-ada-premium-female-female-raw-custom-jumper-wires-40-x-6-150mm.html
- [x+] Panel-mount or inline power switch — ~$5 _(master switch for motor rail)_
  - https://abra-electronics.com/electromechanical/switches/rocker-switches/rs-asw-17d-bl-on-off-dpst-blue-led-rocker-switch-35a-12vdc.html

**Power notes:**

- Keep the Pi and motor system on **separate rails** in phase 0.
- Tie the **grounds together** between Pi and RoboClaw.
- Do **not** power the Pi from the motor rail yet.
- Manual charging is fine for now; autonomous dock charging is a later architecture step.

**Phase 0 subtotal: ~$620** _(deliverable: "it reliably moves")_
**Phase 0 actual total, after tax: 1084.09**

---

## Phase 1 — "It moves and talks"

Add audio/video I/O. Hook up cloud STT/TTS + LLM with a character card so the robot can converse.

### Hardware (~$75)

- [r] Raspberry Pi Camera Module 3 **Wide** (120° FOV) — ~$35 _(wide is what a moving robot needs — 66° standard is too narrow)_
- [S] reSpeaker Flex XVF3800 Circular-4 AI-Powered Microphone Array for Robotics & Embedded Applications - $70
  - https://ca.robotshop.com/products/respeaker-flex-xvf3800-circular-4-ai-powered-microphone-array-for-robotics-embedded-applications?variant=47617041301655

- [r] Small USB speaker — ~$15 _(upgrade later if the robot sounds tinny)_
  - JST-PH 2-pin pigtail STILL NEED TO BUY THIS

**Phase 1 subtotal: ~$75** _(deliverable: "it moves and holds a conversation")_

---

## Phase 2 — "Doesn't bump into things"

_(LED face deferred — aesthetic, non-blocking. Add whenever you're in the mood for personality work; ~$25 for a 64×32 HUB75 matrix + Pi driver when the time comes.)_

### Core sensor suite (~$115)

- [S] 3× VL53L1X ToF distance sensors — ~$18 each (~$55) _(front-left, front-center, front-right. Laser ToF works on fabric/fur/carpet where ultrasonic fails.)_
- [S] TCA9548A multiplexer
- [ ] BNO085 9-axis IMU with onboard fusion — ~$25 _(quaternion orientation directly; no sensor-fusion code to write. SparkFun Qwiic breakout is clean.)_
- [ ] 3× VL53L0X downward-facing cliff sensors — ~$25 total _(stops the robot from launching itself down stairs. Required as soon as it's autonomous.)_
- [ ] Bump sensor rig: microswitches + sprung front bumper — ~$10 _(last-resort "I actually hit something" insurance. Wait-and-see: if ToF + cliff sensors are catching everything, don't wire these up. If the robot bumps things, add them then.)_

**Phase 2 subtotal: ~$115**

---

## Phase 3 — "Sees things"

Decision point: on-device perception acceleration vs. offload to M3.

- [ ] _(Optional)_ Google Coral USB Accelerator — ~$60 _(middle-ground ML inference, if M3 offload starts feeling constrained)_

Otherwise: no hardware purchase. Perception goes to the M3 over LAN.

---

## Phase 4 — "Maps"

- [ ] RPLidar A1 — ~$100 _(2D SLAM, the easy path)_
- [ ] _(Alternative)_ Intel RealSense D435 — ~$200–300 _(depth camera, richer but fiddlier)_

---

## Phase 5 — "Docks itself"

Custom-built, not a purchased product. Needs: IR beacon emitter, IR receivers on the robot, contact pads for charging. Design likely wants 3D printed housing by this point.

---

## Nice-to-have / later

- [ ] 3D printer (Bambu A1 Mini, ~$300) — when you hit a problem where "printing it" is the actual answer
- [ ] Soldering iron (Pinecil, ~$30) — around phase 2+
- [ ] Raspberry Pi Pico 2 (~$5) — when you want hard-real-time motor control with encoders (Pi high-level, Pico low-level)
- [ ] LED face: 64×32 HUB75 matrix + Pi driver (~$25) — when in the mood for personality work
- [ ] ReSpeaker 4-Mic Array (~$65) — if the 2-Mic's range isn't enough for roaming-robot conversations

---

## Sourcing (Canada)

Grouped by domestic supplier to minimize cross-border shipping and customs drama. Check both goBILDA distributors — their inventory doesn't fully overlap.

**Canadian Pi resellers** — Pi Shop Canada, BuyaPi.ca, Elmwood Electronics (Toronto), CanaKit:

- Raspberry Pi 5 16GB
- Official Active Cooler
- Official 27W USB-C PSU
- Pi 5 M.2 HAT
- Pi Camera Module 3 Wide
- ReSpeaker 2-Mic HAT _(often stocked; else via DigiKey/Mouser)_

**goBILDA via Canadian distributors** — RobotShop.ca (Mirabel, QC) or ABRA Electronics:

- All goBILDA chassis parts: 5203 motors, Stealth Wheels, ball caster, U-channel, motor mount brackets, pattern plate, hex shafts/bearings/hardware
- RoboClaw 2x7A _(stocked by RobotShop)_

**Electronics distributors** — DigiKey.ca, Mouser CA, or RobotShop:

- VL53L1X ToF sensors ×3
- VL53L0X cliff sensors ×3
- BNO085 IMU (SparkFun Qwiic breakout)

**Amazon.ca / general retail**:

- M.2 2230/2242 NVMe drive (256GB)
- Samsung Pro Endurance SD card
- Small USB speaker
- 10,000mAh USB-C PD power bank
- 3S LiPo + charger
- Inline fuse holder, power switch, AA batteries
- Dupont jumper wires + breadboard

**Ordering strategy:**

- Consolidate the goBILDA / chassis order into a single RobotShop or ABRA shipment. Even though they're domestic, grouping saves handling time and simplifies tracking.
- If a specific goBILDA SKU isn't stocked at either Canadian distributor, backfill direct from goBILDA.com — the customs hit on a small individual order is painful per-dollar, but acceptable on a missing bracket or two.
- Pi accessories and sensors can come in smaller orders from their respective Canadian sources without meaningful penalty.

**Tariff/customs note:** The US-Canada tariff landscape has been volatile since early 2025. Even with Canadian distributors, sanity-check the landed cost at order time — distributor pricing usually absorbs customs shifts reasonably but not always immediately.

---

## Explicitly NOT buying

- ~~Jetson Orin Nano Super ($250)~~ — M3 MacBook handles perception offload; Pi has better ecosystem
- ~~Custom PCB~~ — never needed for a hobby robot
- ~~Kits of any kind~~
- ~~Pi 4~~ — Pi 5 is enough better to be worth the small price bump

---

## Key decisions & reasoning (so you don't have to re-derive later)

**Power: split rails now, unified rechargeable architecture later.** In phase 0, keep the Pi on its own USB-C power bank and run the motor system from a separate 3S LiPo, with common ground, an inline fuse, and a real power switch. This is intentionally a little hacky, but it isolates motor noise and avoids brownout pain. Later, if the robot gets autonomous docking, move toward a single onboard rechargeable main pack with a dedicated 5V regulator for the Pi and a proper charge path for dock contacts.

**Brain: Pi 5 16GB, not Jetson.** Ecosystem depth wins. M3 MacBook is a capable perception offload target (YOLOv8m at 30+ FPS via CoreML). 16GB over 8GB because ROS2 + multiple services will eat RAM. Pi becomes a body controller cleanly if a Jetson ever joins later.

**Chassis: goBILDA custom build, not a pre-made kit.** Modular metal construction ecosystem (FTC-grade aluminum). Buy components, build your own frame. Infinitely extensible through the same catalog — robot arms, trackdrive conversions, larger platforms all bolt on with the same hex hardware. 5203 Yellow Jacket 312 RPM motors with built-in encoders are the drivetrain choice; plenty of speed at indoor speeds, encoders solve SLAM odometry from day one.

**Chassis size: 288×240mm (11.3″×9.5″).** Fits under most furniture, clears doorways, leaves deck room for Pi + battery + mast + face + future expansion. Smaller starts Tetris-ing fast once lidar mast, LED face mount, and other gear go on.

**LED face: vertical or slightly angled, front-mounted on a frame.** Doesn't consume deck area. Top edge stays below ~250mm so it doesn't obstruct the lidar scan plane.

**Lidar: mast-mounted, ~280–320mm above floor.** Raised so the robot doesn't obstruct its own forward view. Mount on rigid goBILDA vertical U-channel (not round tubing — tubing twists and a wobbly mast produces "SLAM is subtly wrong" bugs that are miserable to diagnose).

**Deck: goBILDA aluminum pattern plate, cut to 288×240mm.** Pre-drilled hole grid aligns with the rest of the ecosystem — no custom drilling, no alignment-error recovery.

**Motor controller: RoboClaw 2x7A, not a dumb H-bridge.** Onboard encoder reading + velocity PID means the Pi commands "go 0.5 m/s" over serial rather than managing PWM and counting interrupts. Much cleaner SLAM odometry, fewer Pi-busy-means-motion-jitter bugs. Worth 3× the price of a Cytron for a robot that'll do real navigation.

**Storage: NVMe on M.2 HAT, not SD card as primary.** SD cards corrupt, wear out, and cause mysterious "it just stopped working" bugs that are miserable to diagnose. NVMe is 5–10× faster and dramatically more reliable. SD stays in the build as emergency backup / initial setup.

**Camera: Pi Camera Module 3 Wide (120°), not standard (66°).** Robots are moving platforms and need peripheral vision. Standard FOV is built for vloggers sitting still.

**Microphone: ReSpeaker 2-Mic HAT, not USB lavalier.** Far-field pickup and beamforming make the difference between "talk to the robot if you're standing next to it" and "talk to the robot from across the room." Essential for conversational UX.

**Distance sensors: VL53L1X laser ToF, not HC-SR04 ultrasonic.** Ultrasonic fails on soft surfaces — couches, cushions, curtains, pet fur, people's clothing all absorb sound and register as "no obstacle" right up until the robot drives into them. ToF works on anything.

**IMU: BNO085, not MPU-6050.** Onboard 9-axis sensor fusion gives you stable quaternion orientation directly. Skipping this means writing DSP code on the Pi to get usable heading out of raw gyro/accel data, and dealing with drift forever.

**Cliff sensors: non-optional as soon as autonomy turns on.** Downward-facing ToF sensors that stop the robot when the floor drops away. Cheap insurance against stair-launch.

**Workload split:**

- Pi: motor control, reactive safety (ultrasonic stop), IMU, SLAM local tracking, wake word — anything latency-sensitive or safety-critical
- M3: object detection, scene understanding, face recognition, Whisper STT
- Cloud: LLM conversation, TTS

**Architecture: treat M3 as a perception microservice.** Pi publishes frames over LAN, M3 publishes detections. Clean network boundary means swapping in a Jetson later is a drop-in, not a rewrite.

**Software: start plain Python + systemd/Docker.** Adopt ROS2 at phase 4 when you actually need Nav2 / TF / rviz, not before.

**MacBook gotcha:** closed-lid sleep will make the robot look catatonic. Fix with clamshell mode, `caffeinate -s`, or Amphetamine before building around "always on."
