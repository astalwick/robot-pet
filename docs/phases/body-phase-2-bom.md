# Body Phase 2 BOM

This is the remaining intended BOM for Body Phase 2. The relay cutoff option has been removed; the intended motor-rail cutoff is the DFRobot MOSFET module below.

Already handled:

- 8-channel I2C multiplexer is installed.
- 3 downward ToF cliff sensors are wired and working.
- Inline fuse holder (20 A fuse) and manual motor master switch are installed.

## Shopping List

Rough costs are CAD, before tax and shipping.

| Qty | Part                                   | Buy from                             | Part / search term                                      | Rough cost | Why                                                                                             |
| --: | -------------------------------------- | ------------------------------------ | ------------------------------------------------------- | ---------: | ----------------------------------------------------------------------------------------------- |
|   3 | VL53L1X time-of-flight sensor breakout | DigiKey Canada                       | **1528-3967-ND / Adafruit 3967**                        |    $55–65  | Front-left, front-center, front-right obstacle distance sensors. STEMMA QT/Qwiic, no soldering. |
|   1 | BNO085 IMU breakout                    | DigiKey Canada                       | **1528-4754-ND / Adafruit 4754**                        |    $34–40  | 9-DOF IMU with onboard sensor fusion.                                                           |
| 3–4 | STEMMA QT / Qwiic cables, 100–200 mm   | DigiKey Canada, Adafruit, or PiShop  | **Adafruit 4210** / **Adafruit 4401**                   |      $5–8  | Plug-in I2C wiring for the forward sensors and IMU, depending on final mount layout.             |
|   3 | Front bumper snap-action microswitches | PiShop Canada, or DigiKey if ordering there too | **PiShop Adafruit 427** or **DigiKey EG4929-ND / E-Switch SS075Q102F035V2A** |      $4–8  | Tiny lever switches for contact fallback. These are logic-level bumper sensors, not industrial limit switches. |
|   1 | Gravity MOSFET Power Controller        | RobotShop Canada                     | **RB-Dfr-731 / DFRobot DFR0457**                        |      ~$6   | Pi-controlled switched-positive motor rail cutoff. Confirmed as suitable for high-side switching. |

Expected parts total: roughly **$105–135 CAD before tax and shipping**.

The DFRobot DFR0457 has been checked twice for this use: once before this review and once against the published DFRobot schematic/docs. It uses a P-channel MOSFET high-side arrangement, with VIN as power input and VOUT as controlled positive output, so it is appropriate for switching the motor battery positive rail here.

### Already Owned / Already Handled

| Item                                      | Notes                                                                                                   |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| 8-channel I2C multiplexer                 | Already installed. Leave the cliff sensors on current mux channels and add forward sensors to free ones. |
| 3 downward ToF cliff sensors              | Already wired and working.                                                                              |
| Inline fuse holder                        | Already installed between LiPo and RoboClaw with a 20 A fuse. Observed draw is well under that, and the 20 A rating is within the wiring's safe current, so keep the existing fuse. |
| Manual motor master switch                | Keep it. It goes before the software switch, so it wins.                                                 |
| JST-SH / STEMMA QT to Dupont cable        | Already owned for Pi GPIO I2C if needed.                                                                |
| 14–16 AWG stranded wire                   | Motor battery → fuse → master switch → MOSFET → RoboClaw.                                               |
| 22–24 AWG Dupont/jumper wire              | Pi GPIO inputs for bump switches and Pi GPIO → MOSFET control side.                                     |
| Crimp tool                                | Needed for motor-power wiring.                                                                          |
| Heat shrink / zip ties / mounting tape    | Strain relief and keeping modules from bouncing around.                                                  |

## Intended Wiring Plan

Motor power path:

```text
LiPo +
  → 15A fuse
  → manual master switch
  → MOSFET VIN
  → MOSFET VOUT
  → RoboClaw BATT+

LiPo −
  → RoboClaw BATT−
  → Pi/MOSFET control GND common
```

Pi-controlled MOSFET path:

```text
Pi GPIO17 or another free GPIO → MOSFET control signal
Pi GND                         → MOSFET control GND
Pi 3.3V, if required            → MOSFET control VCC
```

Do not use GPIO2/GPIO3 because they are the I2C bus for the mux. Do not use GPIO14/GPIO15 because they are the RoboClaw UART.

Front bump switch path:

We do not care *which* bumper is hit, only "all clear" vs "something is touching." So the three SPDT microswitches are wired as a single fail-safe series chain into **one** Pi GPIO, using each switch's **COM** and **NC** terminals (NO is unused).

```text
Pi GPIO ── NC·COM ── NC·COM ── NC·COM ── Pi GND
            sw1        sw2        sw3
```

Enable the Pi internal pull-up on that GPIO. Logic is inverted from the obvious case, on purpose:

- At rest every NC contact is closed, so the chain is an unbroken path to GND → GPIO reads **LOW** = all clear.
- Pressing any switch opens its NC contact, breaking the chain → GPIO floats up → reads **HIGH** = stop.
- A broken wire or unplugged connector also breaks the chain → reads **HIGH** = stop. This is the reason for the series-NC arrangement: a wiring fault fails toward "stop," not toward "blind but pretends fine," which is what a parallel NO wiring would do.

Wiring notes: 22–24 AWG stranded wire, female Dupont on the GPIO/GND end, soldered + heat-shrunk at the switch lugs for strain relief (they live on the bumper and get knocked repeatedly). These switches do not use I2C, so they do not consume channels on the 8-channel mux.

## Intended Behavior

```text
Manual switch OFF:
  Motors are impossible, regardless of software.

Manual switch ON + MOSFET OFF:
  RoboClaw/motors are powered off.

Manual switch ON + MOSFET ON:
  RoboClaw/motors are powered.
```

Software policy will live in **`robot-battery`** ([body-phase-2.md](body-phase-2.md#robot-battery--motor-rail-power)). The rule table there is **brainstorm only — not decided.** One settled-ish idea: energize on wake, read fresh RoboClaw voltage, if pack is low then dashboard warning + rail off (so recharge can be detected on the next wake attempt). Everything else (gamepad idle, disconnect delay, overcurrent, voice hold-open) is still TBD.
