# BOM by Phase

Active bill of materials, grouped by roadmap phase. The older combined list is in [legacy/robot-shopping-list.md](legacy/robot-shopping-list.md).

---

## Body Phase 0 — Reliable Manual Platform

### Brain and accessories


| Qty | Item                            | Vendor  | ~Price | On hand |
| --- | ------------------------------- | ------- | ------ | ------- |
| 1×  | Raspberry Pi 5, 4GB             | Walmart | $120   | Yes     |
| 1×  | Official Active Cooler          | PiShop  | $5     | Yes     |
| 1×  | Pi 5 M.2 HAT                    | PiShop  | $15    | Yes     |
| 1×  | M.2 2230/2242 NVMe drive, 256GB | —       | $35    | No      |
| 1×  | Lexar 64GB 633X CL10 Micro SDXC | Staples | $40    | Yes     |


### Motion / chassis (goBILDA)

Target footprint: 288 × 240 mm. 2WD differential drive plus ball caster.


| Qty | Item                                                                          | Vendor  | ~Price   | On hand |
| --- | ----------------------------------------------------------------------------- | ------- | -------- | ------- |
| 2×  | goBILDA 1120-0009-0240 U-channel, side rails                                  | ABRA    | $13 ea   | Yes     |
| 2×  | goBILDA 1121-0009-0240 U-channel, cross-rails                                 | ABRA    | $12 ea   | Yes     |
| 2×  | goBILDA 1126-0090-0001 steel gusset-plate, 3×3 hole, 4-pack                   | ABRA    | $5.99 ea | Yes     |
| 2×  | goBILDA 5203-2402-0019 Yellow Jacket motor, 312 RPM, 19.2:1, built-in encoder | ABRA    | $40 ea   | Yes     |
| 2×  | goBILDA 3626-0014-0096 96mm Hogback traction wheel                            | goBILDA | $9.99 ea | Yes     |
| 2×  | goBILDA 1401-0043-0036 hub-mount motor mount bracket (optional)               | goBILDA | $8.99 ea | No      |
| 2×  | goBILDA 1309-0016-4008 1309 Series Sonic Hub, 8mm REX bore                    | ABRA    | $7.99 ea | Yes     |
| 2×  | goBILDA 3801-0919-0300 encoder breakout cable, female jumper wires            | goBILDA | —        | Yes     |
| 1×  | RoboClaw 2×7A motor controller                                                | goBILDA | $70      | Yes     |


**CAD:** [Onshape chassis model](https://cad.onshape.com/documents/db4fc3ddecaa1562d76c8ec8/w/723d49fe6bcf38613c12e8f1/e/6953b2a23ad6b97e0ed668ae)

### Caster


| Qty | Item                                                       | Vendor  | ~Price | On hand |
| --- | ---------------------------------------------------------- | ------- | ------ | ------- |
| 1×  | goBILDA 3621-0001-0001 ball caster, 48mm ball              | goBILDA | $7.99  | Yes     |
| 1×  | goBILDA 2814-0016-0008 M8×1.25 mm to goBILDA pattern mount | ABRA    | $5.99  | Yes     |
| 1×  | goBILDA 1141-0001-0001 round-end steel L-bracket, 2-pack   | ABRA    | $5.99  | Yes     |


**Reference:** [RobotShop 2″ swivel caster](https://ca.robotshop.com/products/2-swivel-caster-wheel?qd=bb9a0c18537f90f5776244f9028affed) (alternative)

### Fasteners


| Qty | Item                                                           | Vendor | ~Price   | On hand |
| --- | -------------------------------------------------------------- | ------ | -------- | ------- |
| 2×  | goBILDA 2812-0004-0007 M4×0.7 mm nylock nut, 25-pack           | ABRA   | $2.99 ea | Yes     |
| 2×  | goBILDA 2800-0004-0010 M4 screw, 10 mm, 25-pack                | ABRA   | $3.39 ea | Yes     |
| 1×  | goBILDA 2800-0004-0012 M4 screw, 12 mm, 25-pack                | ABRA   | $3.39    | Yes     |
| 1×  | goBILDA 2800-0004-0016 M4 screw, 16 mm, 25-pack                | ABRA   | —        | Yes     |
| 1×  | goBILDA 2801-0004-0008 zinc washer, 4 mm ID × 8 mm OD, 25-pack | ABRA   | —        | Yes     |


### Power


| Qty | Item                                                                                                                                                                                                                  | Vendor        | ~Price | On hand |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- | ------ | ------- |
| 1×  | 10,000 mAh USB-C PD power bank                                                                                                                                                                                        | Amazon        | $25    | Yes     |
| 1×  | Small 3S LiPo battery pack — [CNHL 2200 mAh 3S Shorty, 2-pack](https://www.amazon.ca/dp/B0FF4J6ZW6)                                                                                                                   | Amazon        | $20–30 | Yes     |
| 1×  | 3S LiPo balanced charger — [2S–3S balance charger](https://www.amazon.ca/dp/B08HN5DZ5Y)                                                                                                                               | Amazon        | —      | Yes     |
| 1×  | Deans to bare wire — [ABRA charging lead](https://abra-electronics.com/interconnects/connectors/power-connector-housing/xt-connectors/cab-t-b-20cm-deans-t-plug-male-to-4mm-banana-plug-charging-lead-rc-cable.html)  | ABRA          | —      | Yes     |
| 1×  | Inline fuse holder                                                                                                                                                                                                    | ABRA          | —      | Yes     |
| 1×  | 20A fuse — [Canadian Tire Maxi fuse](https://www.canadiantire.ca/en/pdp/littelfuse-20a-maxi-fuse-0201504p.html)                                                                                                       | Canadian Tire | $5     | Yes     |
| 1×  | Dupont female-to-female jumper wires — [ABRA 40×6″ pack](https://abra-electronics.com/wire-cable-accessories/wirecable/jumper-wire-assembly/3141-ada-premium-female-female-raw-custom-jumper-wires-40-x-6-150mm.html) | ABRA          | —      | Yes     |
| 1×  | Panel-mount or inline power switch — [ABRA rocker switch](https://abra-electronics.com/electromechanical/switches/rocker-switches/rs-asw-17d-bl-on-off-dpst-blue-led-rocker-switch-35a-12vdc.html)                    | ABRA          | $5     | Yes     |


---

## Body Phase 1 — Audio / Video Body


| Qty | Item                                                                                                                                                                                                                           | Vendor    | ~Price | On hand |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------- | ------ | ------- |
| 1×  | Raspberry Pi Camera Module 3 Wide, 120° FOV                                                                                                                                                                                    | PiShop    | $35    | Yes     |
| 1×  | reSpeaker Flex XVF3800 Circular-4 AI microphone array — [RobotShop](https://ca.robotshop.com/products/respeaker-flex-xvf3800-circular-4-ai-powered-microphone-array-for-robotics-embedded-applications?variant=47617041301655) | RobotShop | $70    | Yes     |
| 1×  | Small USB speaker                                                                                                                                                                                                              | PiShop    | $15    | Yes     |
| 1×  | JST-PH 2-pin pigtail                                                                                                                                                                                                           | —         | —      | Yes     |


---

## Body Phase 2 — Local Safety Sensing


| Qty | Item                                                                                                                                                           | Vendor            | ~Price     | On hand |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------- | ---------- | ------- |
| 3×  | VL53L1X ToF distance sensor                                                                                                                                    | Seeed / RobotShop | $18 ea     | Yes     |
| 1×  | TCA9548A I2C multiplexer                                                                                                                                       | Seeed / RobotShop | —          | Yes     |
| 1×  | BNO085 9-axis IMU with onboard fusion                                                                                                                          | —                 | $25        | No      |
| 3×  | VL53L0X downward cliff sensor                                                                                                                                  | —                 | ~$25 total | Yes     |
| 1×  | Bump sensor rig: microswitches + sprung front bumper                                                                                                           | —                 | $10        | No      |
| 1×  | High-side MOSFET DC power switch, 3.3 V logic, 3S LiPo / 20 A motor rail — [Gravity module](https://ca.robotshop.com/products/gravity-mosfet-power-controller) | RobotShop         | $8         | No      |
| 1×  | GPIO control lead / connector for Pi-to-MOSFET enable                                                                                                          | —                 | —          | No      |


**Alternative MOSFET:** [CNC4PC high-side adapter](https://www.cnc4pc.com/shop/adapter-pfet-mosfet-power-switch-adapter-48v-20a-high-side-switch-1781) (probably overkill)

---

## Body Phase 3 — Perception Offload


| Qty | Item                                    | Vendor | ~Price | On hand |
| --- | --------------------------------------- | ------ | ------ | ------- |
| 1×  | Google Coral USB Accelerator (optional) | —      | $60    | No      |


Default path is still MacBook offload over LAN; this phase may need no extra robot hardware.

---

## Body Phase 4 — SLAM and Localization


| Qty | Item                               | Vendor | ~Price   | On hand |
| --- | ---------------------------------- | ------ | -------- | ------- |
| 1×  | RPLidar A1                         | —      | $100     | No      |
| 1×  | Intel RealSense D435 (alternative) | —      | $200–300 | No      |


---

## Body Phase 5 — Autonomous Navigation

No new hardware identified yet. Depends on mapping sensor, local safety sensors, and ROS 2 / Nav2 software.

---

## Body Phase 6 — Docking and Self-Recharge

Custom build, not a purchased product. Expected materials:


| Qty | Item                                                                    | On hand |
| --- | ----------------------------------------------------------------------- | ------- |
| 1×  | IR beacon emitter or other close-range dock signal                      | No      |
| 1×  | IR receivers or equivalent dock sensors on the robot                    | No      |
| 1×  | Charging contact pads                                                   | No      |
| 1×  | Dock housing (likely 3D printed)                                        | No      |
| 1×  | Power architecture parts for safe dock charging (TBD before this phase) | No      |


---

## Body Phase 7+ — Expansion Platform

Possible later purchases:


| Qty | Item                                                                             | ~Price | On hand |
| --- | -------------------------------------------------------------------------------- | ------ | ------- |
| 1×  | 3D printer, Bambu A1 Mini or similar                                             | $300   | Yes     |
| 1×  | Soldering iron, Pinecil or similar                                               | $30    | No      |
| 1×  | Raspberry Pi Pico 2                                                              | $5     | No      |
| 1×  | LED face: 64×32 HUB75 matrix + Pi driver                                         | $25    | No      |
| 1×  | Extra microphone array (if current mic is insufficient for roaming conversation) | —      | No      |


---

## Personality / agency phases

Personality phases mostly reuse Body Phase 1 audio/video and capabilities from later body phases:


| Phase               | Hardware used                                                   |
| ------------------- | --------------------------------------------------------------- |
| Personality Phase 1 | Body Phase 1 mic and speaker (STT → cloud LLM → TTS)            |
| Personality Phase 2 | Body Phase 0 motion for tiny bounded tools                      |
| Personality Phase 5 | LED face from Body Phase 7+ if expression work pulls it forward |


