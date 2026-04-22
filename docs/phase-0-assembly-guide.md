# Phase 0 — You Got Your Parts. Now What?

**Goal:** A robot chassis that reliably drives forward, backward, and turns on command from your laptop over SSH. No camera, no mic, no autonomy — just "I can make this thing move."

**Assumptions:** Your Pi 5 is flashed, booted, and SSH-accessible on your network. OS is on the SD card.

---

## The Big Picture (Assembly Order)

You're building from the ground up, literally. The order matters because some things are much harder to reach once other things are bolted on top.

1. **Mechanical chassis** — frame, motors, wheels, caster
2. **UART fix** — free the serial port from Bluetooth so the Pi can talk to the RoboClaw
3. **Wiring** — RoboClaw to motors, encoders to RoboClaw, Pi UART to RoboClaw, power rails
4. **RoboClaw configuration** — low-voltage cutoff, encoder CPR, PID tuning, motor direction, current limits
5. **Software** — serial comms, motor commands, keyboard teleoperation
6. **Integration test** — wheels on the ground, driving around under control

---

## Step 1: Build the Chassis

### What you're doing

Assembling the goBILDA frame: two side-rail U-channels connected by two cross-rail U-channels, motors mounted in the side rails, wheels on the motor shafts, ball caster underneath the rear cross-rail, gusset plates reinforcing the corners.

### Suggested order

**Motors into side rails first.** The 5203 motors drop into the U-channel from the inside. Bolt them in place with M4 screws before the cross-rails go on — once the frame is assembled, reaching inside the U-channel to align motor mounting holes is annoying.

**Wheels onto motor shafts.** Press the Sonic Hubs onto the 8mm REX shafts, then mount the 96mm Hogback wheels onto the hubs. Easier to do this with the motor held in your hand or loosely mounted than with the full frame assembled.

**Cross-rails to side rails.** Use the gusset plates at each corner. M4 screws + nylocks. Snug, not gorilla-tight — you may need to adjust spacing later. You're forming a rectangular frame: side rails run front-to-back, cross-rails connect them left-to-right.

**Ball caster underneath.** The caster mounts to the rear cross-rail via the L-bracket and M8 adapter. The goal is to get the chassis roughly level when it sits on all three contact points (two drive wheels + caster ball).

### What success looks like

Set the assembled chassis on a flat surface (table or floor). The two drive wheels and the caster ball all touch. The chassis sits roughly level — no obvious tilt. You can spin both wheels freely by hand. The frame feels rigid, not wobbly.

### Level check

If the chassis tilts noticeably (more than a couple of degrees), add washers or spacers under the caster mount to raise or lower it. You're aiming for "eyeball level," not machinist-precision. Anything under ~2° of tilt is fine.

Worth getting this right now rather than later: every sensor you add in Phase 2 assumes the chassis is level. ToF sensors aimed "forward" that are actually pointed a few degrees downward give phantom obstacles. An IMU that thinks "level" is tilted gives bad odometry. Getting the caster height right in Phase 0 means Phase 2 sensor calibration is trivial instead of mysterious.

---

## Step 2: Free Up UART for the RoboClaw

### Why this matters

The Pi 5 assigns its primary UART (the one on GPIO pins 14/15) to Bluetooth by default. You need that UART for serial communication with the RoboClaw. If you skip this step, serial commands to the RoboClaw will go nowhere and you'll spend hours thinking something is wired wrong.

### What you're doing

Edit `/boot/firmware/config.txt`, add `dtoverlay=disable-bt`, and reboot. This frees UART0 for general use and disables Bluetooth (which you don't need on this robot).

### What success looks like

After reboot, `ls /dev/serial0` exists and points to the UART device. You can confirm with `ls -l /dev/serial0` — it should symlink to something like `/dev/ttyAMA0`.

---

## Step 3: Wire Everything

### The wiring map

You have two independent power rails and one signal path. Here's what connects to what:

**Motor power rail (3S LiPo):**
```
LiPo (+) → inline fuse (20A) → power switch → RoboClaw motor power (+)
LiPo (−) → RoboClaw motor power (−)
```

**Pi power rail (USB-C power bank):**
```
Power bank USB-C → Pi 5 USB-C power input
```

**Critical: tie the grounds together.**
```
RoboClaw logic ground ↔ Pi ground (any GND pin on the GPIO header)
```
Without common ground, serial communication between the Pi and RoboClaw will be unreliable or won't work at all. This is a single jumper wire but it's easy to forget.

**Signal: Pi UART → RoboClaw serial**
```
Pi GPIO 14 (TX) → RoboClaw S1 (RX)
Pi GPIO 15 (RX) → RoboClaw S1 (TX)
Pi GND → RoboClaw GND (this is the same common ground wire)
```

**Motor connections (RoboClaw → motors):**
```
RoboClaw M1A / M1B → Left motor leads
RoboClaw M2A / M2B → Right motor leads
```

**Encoder connections (motors → RoboClaw):**
```
Left motor encoder cable → RoboClaw encoder 1 header
Right motor encoder cable → RoboClaw encoder 2 header
```
The goBILDA encoder breakout cables convert the motor's 4-pin JST connector to individual jumper wires. Each encoder has 4 wires: 5V (or 3.3V), GND, Channel A, Channel B. Match these to the RoboClaw encoder header pins — the RoboClaw manual labels them clearly.

### Suggested wiring order

1. **Encoders first.** Small wires, fiddly connectors — easier before the power wiring is in the way.
2. **Motor leads to RoboClaw.** Just two wires per motor.
3. **Pi UART + ground to RoboClaw.** Three jumper wires.
4. **Motor power rail.** LiPo → fuse → switch → RoboClaw. Leave the LiPo disconnected (or the switch off) until you've configured the RoboClaw.

### What success looks like

Everything is connected but **nothing is powered on yet** except the Pi (running off its USB-C power bank). You can visually trace each connection. No bare wire ends touching each other. The LiPo is either disconnected or the power switch is off.

---

## Step 4: Configure the RoboClaw

### Why before powering motors

The RoboClaw ships with defaults that don't match your motors or battery. If you power up and start sending "go" commands with wrong settings, you'll get confusing behavior at best and a damaged LiPo at worst.

### Configuration method

You can configure the RoboClaw by sending serial commands from the Pi using BasicMicro's Python library (available on GitHub). The alternative is BasicMicro's "Motion Studio" desktop app, but that's Windows-only — you'd need a VM on your Mac. The serial-from-Pi path works fine and keeps everything in one place.

### The five things to configure (in this order)

**1. Low-voltage cutoff — do this first, it has safety consequences.**

Your 3S LiPo is permanently damaged if any cell drops below ~3.0V. Set the RoboClaw's minimum battery voltage to **9.6V** (3.2V/cell — conservative). If you skip this and get absorbed in testing, you'll drain the pack below safe voltage. A damaged LiPo puffs up and becomes a fire risk. This is the only part of Phase 0 where getting it wrong has a real physical consequence.

**2. Encoder CPR (counts per revolution).**

Your 5203-2402-0019 motors have 537.7 PPR at the output shaft. The RoboClaw runs in full quadrature mode by default (counts all 4 edges per encoder cycle), so the effective resolution is **~2,150 counts per revolution**. That's the number the RoboClaw needs. With your 96mm wheels, this works out to ~7,100 counts per meter of travel — sub-millimeter resolution that'll serve you well all the way through SLAM.

**3. Motor direction.**

Your two motors face opposite directions on a differential-drive robot. To go straight, one spins clockwise and the other counter-clockwise. The RoboClaw can flip motor direction in config. If you skip this, the robot will spin in circles when you tell it to go forward, and you'll think something is wired wrong.

**4. Velocity PID tuning.**

Default PID values won't match your motor/wheel/weight combination. The RoboClaw has a built-in auto-tune function that gets you in the ballpark — start there. Then refine manually: command a target velocity, watch what the encoders report, adjust gains. Budget a dedicated evening for this. Start with low gains and work up. Aggressive PID gains cause the motors to oscillate (buzzing/jerking) — back off if you see that.

**5. Current limits.**

Set per-motor limits to ~5A. Your motors draw 1–3A under normal use and up to 9.2A at absolute stall. A 5A limit protects the motors if the robot gets stuck against a wall (the RoboClaw will cut power instead of letting the motor stall-draw indefinitely).

### What success looks like

With the LiPo connected and switch on, the RoboClaw powers up (status LEDs illuminate). You can read the battery voltage back from the RoboClaw over serial and it reports ~11.1V (or whatever your LiPo is sitting at). The low-voltage cutoff is set and you can read it back to confirm. Encoder counts change when you manually rotate a wheel by hand.

---

## Step 5: First Motor Test (Bench, Wheels in the Air)

### What you're doing

With the chassis on the bench (wheels not touching the ground), send basic motor commands from the Pi and verify that both motors spin, encoders report movement, and direction is correct.

### The process

Install the BasicMicro RoboClaw Python library on the Pi. Write (or grab from their examples) a short script that:

- Opens the serial connection to the RoboClaw
- Commands motor 1 forward at low speed
- Reads encoder 1 counts and prints them
- Stops motor 1
- Repeats for motor 2

Run it. Watch the wheels. Both should spin. Encoder counts should increase in the direction you expect. If a motor spins the wrong way, flip its direction in the RoboClaw config (don't swap wires — config is cleaner).

Then test "go forward" (both motors, same commanded velocity, opposite physical directions since they face each other). Both wheels should spin in the direction that would push the robot forward if it were on the ground. Test "turn left" and "turn right" — one wheel forward, one backward.

### What success looks like

Both motors respond to commands. Encoder counts track smoothly (no jumps, no stuck values). Forward/backward/left/right all produce the correct wheel behavior. Motor speed is controllable — you can command slow and fast and see the difference.

---

## Step 6: Keyboard Teleoperation

### What you're doing

Writing a simple keyboard-driven control script so you can drive the robot around from your laptop over SSH. This is your integration test: everything — Pi, serial, RoboClaw, motors, encoders, power — working together.

### The process

Write a Python script that reads keyboard input (WASD or arrow keys) and translates to motor commands: W = forward, S = backward, A = turn left, D = turn right, space = stop. Nothing fancy — just enough to drive it around. A `curses`-based terminal UI works well for this, or any keyboard input library.

Start with the robot still on the bench (wheels in the air) and verify all directions map correctly. Then put it on the floor.

### What success looks like

You SSH into the Pi, run your teleop script, and drive the robot around the room from your laptop keyboard. It goes where you tell it. It stops when you tell it to stop. It doesn't drift or pull to one side (if it does, your PID tuning or motor direction config needs adjustment). The robot can navigate around a few obstacles (chair legs, a shoe) under manual control.

**This is the Phase 0 deliverable: a reliable, manually-driven robot platform.**

---

## Ongoing Habits

**After every session:** Put the LiPo into storage mode on your charger (3.8V/cell). LiPo batteries degrade if left fully charged or fully drained. Your charger has a storage mode — use it every time. This is how the battery lasts years instead of months.

**Power bank behavior:** Watch for your USB-C power bank shutting off during idle periods. Some banks detect low current draw and cut power. If your Pi randomly dies while idling, this is probably why — not a software crash. Check if the bank has an "always-on" mode. Workaround: a background script that periodically pulses a GPIO pin into a small load (LED + resistor).

**Be kind to the SD card.** You're running on SD for now, and SD cards wear out from frequent small writes. Avoid aggressive logging (sensor data, motor telemetry) — if you need to log, write to tmpfs (RAM) and flush periodically. Keep configs and code in git so that if the card does corrupt, you're back up in 30 minutes, not a day of archeology.

---

## Quick Reference: Your Numbers

| Parameter | Value |
|---|---|
| Motor model | goBILDA 5203-2402-0019 |
| Gear ratio | 19.2:1 |
| No-load speed | 312 RPM @ 12V |
| Encoder PPR (output shaft) | 537.7 |
| Encoder CPR (quadrature, for RoboClaw) | ~2,150 counts/rev |
| Wheel diameter | 96mm |
| Counts per meter | ~7,100 |
| Max speed (theoretical) | ~1.57 m/s |
| Comfortable operating speed | 0.3–0.5 m/s |
| Battery | 3S LiPo, 11.1V nominal |
| Low-voltage cutoff | 9.6V (3.2V/cell) |
| Motor current limit | 5A per motor |
| Pi UART | GPIO 14 (TX), GPIO 15 (RX) |
| RoboClaw serial | S1 header |
| Baud rate (RoboClaw default) | 38400 |
