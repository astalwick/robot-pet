# Robot Build — Gotchas, Risks & Critical Config

**Purpose:** Things that will bite you if you don't know about them going in. Organized by phase so you hit the right warnings at the right time.

Read the relevant section *before* you start each phase. Future-you will thank present-you.

---

## Phase 0 — "It reliably moves"

### ⚠️ ROBOCLAW CONFIGURATION — READ THIS BEFORE YOU POWER MOTORS

The RoboClaw does not work out of the box with your motors. Before you send your first "go forward" command, you must configure:

1. **Low-voltage cutoff: SET THIS FIRST.**
   Your 3S LiPo is permanently damaged if any cell drops below ~3.0V (9.0V pack voltage). The RoboClaw ships with either no cutoff or a default that doesn't match your battery.
   - Set the cutoff to **9.6V** (3.2V/cell, conservative).
   - If you forget this and get absorbed in testing, you will drain the pack below safe voltage. A damaged LiPo puffs up and becomes a fire hazard.
   - This is the only part of the build with a real safety consequence if you get it wrong.

2. **Encoder counts per revolution (CPR).**
   The 5203-2402-0019 motors (19.2:1 ratio) have **537.7 PPR at the output shaft** (28 encoder pulses × 19.2 gear ratio). In full quadrature mode (which is what the RoboClaw uses by default — counting all 4 edges), the effective resolution is **~2,150 counts per revolution**. That's the number to enter in the RoboClaw config. With 96mm wheels, this gives ~7,100 counts per meter — sub-millimeter odometry resolution, plenty for SLAM.

3. **Velocity PID tuning.**
   Default PID values won't match your motors + wheel size + robot weight. Budget an evening for tuning. Start with low gains and work up. The RoboClaw's built-in auto-tune can get you in the ballpark.

4. **Motor direction.**
   Two motors on opposite sides of a differential-drive robot spin in opposite directions to go straight. The RoboClaw handles this in config, but if you skip it, the robot spins in circles and you'll think something is wired wrong.

5. **Current limits.**
   Set per-motor current limits to something sane (~5A is generous for these motors at ~1-3A typical draw). Protects against stall conditions where the motor draws up to 9A.

**Configuration method:** You can configure the RoboClaw via BasicMicro's Motion Studio (Windows — you'd need a VM on your Mac) or by sending serial commands from the Pi. The serial-from-Pi path works fine. The RoboClaw Python library from BasicMicro covers all of this.

---

### Pi 5 UART is claimed by Bluetooth

When you try to talk to the RoboClaw over serial (GPIO 14/15, UART0), it won't work. Pi 5 assigns UART0 to Bluetooth by default.

**Fix:** Add `dtoverlay=disable-bt` to `/boot/firmware/config.txt` and reboot. This frees UART0 for the RoboClaw. Alternatively, use one of the Pi 5's other UART peripherals (it has several, unlike Pi 4) — but disabling Bluetooth is simpler since you probably don't need BT on the robot.

This is a 30-second fix once you know. It's a 3-hour debug session if you don't.

---

### Power bank auto-shutoff

Many power banks detect low current draw and cut power, assuming nothing is connected. When the Pi is idle (SSH prompt, no motor commands), draw can dip low enough to trigger this. The Pi just dies — no warning, no graceful shutdown.

You'll think it's SD card corruption, a kernel panic, a software crash. It's the power bank deciding you're done.

**Check:** Whether your Anker 10K 30W bank has an "always-on" or "trickle charge" mode.

**Workaround if it doesn't:** A small background script/service that periodically pulses a GPIO pin into a dummy load (an LED + resistor is enough) to keep current above the shutoff threshold.

---

### Ball caster height must match drive wheel height

Drive wheels: 96mm diameter (48mm radius to axle). The ball caster (48mm ball) on L-bracket + M8 adapter must set the chassis level.

**Verify physically** when parts arrive: set the assembled chassis on a flat surface and check with a level. If it tilts, add spacers to the caster mount.

**Why this matters beyond Phase 0:** Every sensor you add in Phase 2 assumes the chassis is level. ToF sensors aimed "forward" that are actually aimed 5° downward give phantom obstacles. An IMU that thinks level is tilted gives bad odometry. Get this right now and Phase 2 calibration is trivial.

---

## Phase 1 — "It moves and talks"

### ⚠️ ReSpeaker 2-Mic HAT may not work on Pi 5

This is the biggest hardware risk in the whole Phase 0–3 plan.

The ReSpeaker 2-Mic HAT was designed for Pi 3/4. The Pi 5 completely rearchitected its I2S audio subsystem. There are documented compatibility issues — some people have gotten it working with manual driver compilation and kernel patches, others haven't. Seeed Studio's official driver support historically lags behind new Pi releases.

**Before you buy it:** Search "ReSpeaker 2-Mic Pi 5" and check the current state of driver support.

**Fallback:** A USB microphone array. Simpler (no HAT, no GPIO conflict, no driver issues), at the cost of slightly worse far-field pickup. For Phase 1 "proof of conversation" this is perfectly fine. If you want serious far-field performance later, revisit the mic solution then.

---

### GPIO stacking header (needed for Phase 2 transition)

The ReSpeaker HAT physically covers the entire 40-pin GPIO header. In Phase 1 this is fine — UART for the RoboClaw passes through. But in Phase 2, you need access to ~6 GPIO pins for ToF sensor XSHUT lines.

**Plan ahead:** When you install the ReSpeaker, use a GPIO stacking header (~$3) between the Pi and the HAT. This gives you a second row of accessible pins on top. If you install the HAT flat now, you'll have to remove it and add the stacking header later.

---

## Phase 2 — "Doesn't bump into things"

### ToF sensor I2C address conflict

The VL53L1X (forward-facing) and VL53L0X (cliff) sensors all ship with the same default I2C address: **0x29**. You have six of them.

**Fix:** Use the XSHUT pin on each sensor. At boot, hold all XSHUT pins low (all sensors off). Bring them up one at a time, assigning each a unique I2C address before enabling the next. This needs 6 GPIO pins (one per sensor) and a few lines of initialization code. It's a well-documented pattern with Python library support — not hard, but you have to do it.

**Alternative:** A TCA9548A I2C multiplexer (~$5). One extra board, but you avoid the XSHUT dance and free up 6 GPIO pins. Worth considering if pin budget gets tight.

---

### I2C bus speed

Default I2C on the Pi is 100kHz. With 7 devices (6 ToF + IMU), you may need to increase to 400kHz to get acceptable polling rates. Each ToF reading takes a few ms — at 100kHz with 6 sensors, you might only get 15-20Hz total update rate. At 400kHz, 50Hz+ is comfortable.

**Fix:** `dtparam=i2c_baudrate=400000` in `/boot/firmware/config.txt`.

---

### Motor battery sleep cutoff

The RoboClaw draws logic current even when the motors are stopped. If the manual motor switch is accidentally left on for days, the 3S LiPo can continue discharging even though the robot is not moving.

**Fix:** Add a Pi-controlled high-side MOSFET switch in the motor battery positive rail, after the existing fuse and manual switch and before the RoboClaw. Use RoboClaw main-battery voltage telemetry while the motor rail is awake. If voltage is low, stop motion first, wait briefly, then cut the motor rail.

Do not use a low-side switch here. Keep the RoboClaw/Pi ground reference continuous, and keep the manual switch as the trusted hard cutoff.

---

## Phase 3 — "Sees things"

### WiFi dropout between Pi and M3 needs graceful degradation

The architecture has the Pi shipping camera frames to the M3 over your home network. WiFi will drop — microwave interference, router reboots, the robot drives behind a wall.

Your Phase 2 sensors (ToF, cliff, IMU) are all local to the Pi, so safety-critical obstacle avoidance works without the network. But the perception layer (object detection, scene understanding) goes away when WiFi drops.

**Design the software so "M3 unreachable" is a handled state:** stop and wait, revert to local-sensor-only wander, or announce "I can't see right now" — not an unhandled exception that crashes the behavior loop.

---

### M3 MacBook sleep

Already noted in the shopping list, but repeating here because it will catch you: closed-lid sleep makes the robot go catatonic mid-conversation.

**Fix before you start Phase 3 development:** `caffeinate -s` in a terminal, or install Amphetamine from the App Store. Clamshell mode (external display + keyboard connected) also works.

---

## General — all phases

### SD card strategy

You're starting on SD card (NVMe deferred). This is fine, but:

- **Script your environment setup early.** If the SD corrupts — and it might — you want a 30-minute rebuild, not a day of "what packages did I install."
- **Keep configs in git** from day one.
- **Don't log aggressively to SD.** Frequent small writes are what kills SD cards. If you're logging sensor data or motor telemetry, log to tmpfs (RAM) and flush periodically, or wait for the NVMe.

### LiPo storage charging

When you're done for the night, put the LiPo into **storage mode** on your charger (brings cells to 3.8V each). LiPo batteries degrade if left fully charged. Your HTRC charger has this mode — use it every time. This is how the battery lasts years instead of months.
