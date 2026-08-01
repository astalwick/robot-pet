# Remaining Code Review

Review of everything outside the voice stack and motor/control stack:
telemetry hub (`robot_telemetry.py`, `telemetry/*`), sensors (`robot_sensors.py`,
`drivers/range.py`, `drivers/imu.py`, `config/sensors.py`), battery services
(`robot_battery.py`, `robot_pi_battery.py`, `drivers/ups_hat_e.py`), camera and
vision (`robot_camera.py`, `robot_vision.py`, `drivers/camera.py`,
`config/vision.py`), dashboards (`robot_dashboard.py`, `robot_web_dashboard.py`),
`robot_brain.py`, `lib/log.py`, and `scripts/redeploy-robot.sh`. No code changes —
findings only.

Overall the infrastructure is thoughtfully layered — single camera owner, config
hot-reload, atomic config writes — but the telemetry hub is a single point of
failure with surprisingly wide blast radius. Items below are ordered roughly by
how much I'd care.

---

## Bugs

### 1. One stalled subscriber wedges the whole telemetry hub — and the LiPo cutoff with it

`TelemetryHub.broadcast` writes to each subscriber sequentially and awaits
`drain()` with no timeout:

```python
# robot_telemetry.py — broadcast
for writer in list(self.subscribers):
    try:
        writer.write(encoded)
        await writer.drain()
    except (ConnectionError, OSError):
        dead.append(writer)
```

A subscriber that connects but stops reading (SSH dashboard suspended with
Ctrl-Z, a hung web-dashboard thread) fills the socket and transport buffers, and
then `drain()` blocks forever. The broadcast loop is the only sender, so *all*
subscribers stop getting snapshots. The nastiest downstream effect:
`robot_battery` drives the MOSFET rail and low-voltage cutoff entirely from these
snapshots — while the hub is wedged, the rail freezes in its last state and the
LiPo cutoff is effectively disabled. A per-writer `wait_for` around drain (or
dropping slow subscribers) would contain this.

### 2. VL53L1X "last good" readings have no staleness bound

When the sensor isn't ready, `RangeDriver._read_locked` returns the previous
reading with `ok=True`:

```python
# drivers/range.py — _read_locked
if not sensor.data_ready:
    previous = self._last_good.get(config.name)
    if previous is not None:
        return previous
```

If a VL53L1X silently stops ranging (power glitch resets it to idle — no
exception, `data_ready` just stays `False`), the service republishes the frozen
distance as a fresh, healthy reading forever. These readings feed the
cliff/forward safety gate, so a dead forward sensor showing its last "500 mm,
floor present" keeps forward motion enabled. One reuse to paper over a missed
cycle is fine; unlimited reuse needs an age cap that flips `ok` to `False`.

### 3. A malformed telemetry line permanently freezes both dashboards

`subscribe()` yields `decode_json_line(line)` with no error handling, and a
partial line is realistic — if the hub dies mid-write, the tail of the stream is
a truncated JSON line:

```python
# telemetry/socket_client.py — subscribe
for line in file_obj:
    if line:
        yield decode_json_line(line)
```

The `JSONDecodeError` escapes the generator and kills the consumer: the TUI's
`_telemetry_thread` and the web dashboard's `TelemetrySubscriberThread` both die
silently, leaving the dashboard frozen on the last snapshot with no log line.
`robot_battery` uses the same iterator — there a crash at least takes the service
down so systemd restarts it, but the dashboards just go quietly stale. Catching
`ValueError` alongside `OSError` in the reconnect loop would fix all three
consumers.

### 4. The hub's publisher socket has a 64 KB line limit that voice timeline updates can plausibly hit

`asyncio.start_unix_server` is called without `limit=`, so `readline()` in
`_handle_publisher` raises once a single JSON line exceeds 64 KiB. The voice
`source_update` carries the full timeline (per the voice review, ~700 events plus
~600 level samples), which is in the tens-of-KB range — uncomfortably close to
the cap. When it's exceeded, the update is dropped with a generic warning and
every subsequent publish fails the same way (each `publish_message` is a fresh
connection), so voice telemetry would vanish with confusing "publisher update
failed" spam. Passing a larger `limit` is a one-line fix.

### 5. Pi UPS shutdown is attempted exactly once, ever

In `PiBatteryService.tick`, the guard sets `shutdown_requested = True` before
calling `_shutdown`, and nothing ever resets it. If `sudo shutdown -h now` fails
(sudoers drift, transient error — precisely the environment-dependent thing that
does fail), the error is logged once and the service happily keeps publishing
telemetry until the UPS browns out — the one outcome this service exists to
prevent. Retrying on subsequent low-voltage ticks would make it robust.

### 6. The TUI dashboard's `LOG_COMMAND` is missing four services

The comment above it says "Keep aligned with `systemctl enable` in setup.sh (all
shipped robot *.service units)", but the list in `robot_dashboard.py` omits
`robot-motion`, `robot-battery`, `robot-pi-battery`, and `robot-voice` — all
shipped and enabled in `setup.sh`. The copy in `robot_web_dashboard.py` has the
full list, which confirms this one just fell behind. So the SSH dashboard
silently shows no logs from the motion service or either battery service —
exactly the ones you'd want during a drive problem.

### 7. A failed redeploy permanently skips service restarts for that commit

`redeploy-robot.sh` fast-forwards the repo *first*, then runs tests. If tests
fail, `set -e` aborts with the new code checked out but old code still running.
There's no marker of the failed deploy, so:

- Re-running redeploy prints "Already up to date" and exits 0 without restarting
  anything.
- After you push a fix and redeploy, `changed_files` is diffed only from the
  *new* `old_rev` — services affected by the earlier (failed) commit's changes
  are never restarted and keep running code from two deploys ago, while the
  working tree looks current.

Recording the last *successfully deployed* rev and diffing from that would close
the gap.

### 8. Redeploy restart-planning has stale dependency mappings

Two concrete holes in `plan_service_restarts`:

- `src/telemetry/*` only restarts `robot-telemetry`, `robot-battery`, and
  `robot-pi-battery` — but `telemetry/messages.py` and `socket_client.py` are
  imported by robot-motion, gamepad-teleop, robot-sensors, robot-vision,
  robot-voice, and the web dashboard. A schema change deploys with half the
  fleet still speaking the old dialect.
- `src/drivers/imu.py` falls into the generic `src/drivers/*` bucket, which
  restarts brain/teleop/camera/vision/voice — everything *except*
  `robot-sensors`, its only consumer.

### 9. Bad sensors-form input crashes the handler instead of returning 400

In `robot_web_dashboard.config_apply`, `merge_sensors_form_patch` runs outside
the validation `try`, and it calls `int(patch["cliff_trip_above_mm"])` directly —
a non-numeric value from the form raises `ValueError` and surfaces as an aiohttp
500 rather than the friendly `Invalid sensors config: ...` 400 every other bad
input gets.

### 10. Boolean thresholds parse as valid millimeters

`config/sensors.py` `_parse_positive_mm` checks `isinstance(value, int)`, which
accepts `True` — so `"trip_above_mm": true` silently becomes a 1 mm threshold.
The same file explicitly excludes bool for `offset_mm`, so the intent exists; it
just wasn't applied to the safety-critical fields.

---

## Weird code / smells

- **The two dashboards share ~80 lines of copy-pasted infrastructure** —
  `stream_command_output`, `restart_gamepad_teleop`, `restart_robot_motion`,
  `redeploy_command`, and `LOG_COMMAND` are duplicated verbatim between
  `robot_dashboard.py` and `robot_web_dashboard.py`, and the drift already
  happened (#6). This is past the three-use threshold in spirit: two copies that
  *must* stay in sync and demonstrably don't.
- **Dead things.** `BUTTON_FIELDS` in `messages.py` is never used. `is_stale` is
  fully dead; `stale_label` is used only by its own test. `GROVE_MUX_ADDRESS` is
  used only by a diagnostic script (fine, but it lives in the driver).
- **The hub knows the voice schema.** `_handle_publisher` special-cases
  `source == "voice"` to carry the previous timeline forward. Besides being an
  odd layering (the generic hub reaching into one publisher's payload), a
  timeline received once persists forever — if voice restarts and stops sending
  timelines, the hub keeps republishing the stale one.
- **Redundant condition, duplicated twice.** `motor_battery_value` guards with
  `motion_source.get("last_seen") is not None or motion_source.get("stale") is
  False` — the second clause is implied by the first (`_source_status` can't
  produce `stale=False` with `last_seen=None`). The same "motion owns it once
  seen" logic is re-implemented independently in
  `robot_dashboard.apply_snapshot`.
- **Driver logs go nowhere.** `drivers/range.py`, `drivers/camera.py` use
  `logging.getLogger(__name__)`, but `setup_logging` only configures the named
  service logger — root is never configured. So `log.info("camera configured:
  %s", ...)` in the camera driver is silently dropped (Python's last-resort
  handler is WARNING+), and driver warnings arrive unformatted on stderr. That
  camera-configuration line is exactly what you'd want in the journal when
  debugging modes.
- **`voice_update` is a 40-parameter function** with a 25-branch `if x is not
  None` ladder. The voice review flagged its 22-parameter sibling; this is the
  other half of the same shape.
- **Boundary inconsistency**: `pi_battery_status` treats warning as `<=`,
  `motor_battery_status` as `<`. Cosmetic, but the kind of thing that confuses
  threshold-tuning later.
- **`_record_history(self, snapshot, gamepad_live)`** in `robot_dashboard.py` —
  the caller now passes `motor_live`; the parameter name is a fossil from before
  robot-motion existed.
- **`except (ConnectionResetError, ConnectionError)`** in `robot_camera.py` and
  the web dashboard — `ConnectionResetError` is a subclass of `ConnectionError`;
  the tuple is half redundant.
- **Debounce resets on telemetry hiccups.** In `robot_battery._handle_snapshot`,
  any snapshot without a fresh voltage sets `low_voltage_seen_at = None`. If
  motion telemetry flaps stale/fresh while the pack sits below cutoff, the 2 s
  debounce keeps restarting and the cutoff is postponed indefinitely. Narrow, but
  it's the protective path.

---

## Inefficiencies

- **The hub spawns ~10 subprocesses per second on its event loop.**
  `sample_pi_health` runs `vcgencmd measure_temp` and `vcgencmd get_throttled`
  via blocking `subprocess.run` inside `_broadcast_loop`, 5×/second, each with a
  0.2 s timeout — up to 0.4 s of event-loop stall per tick in the worst case, and
  constant fork/exec churn on the Pi. Sampling SoC health at 1 Hz (or reading
  sysfs instead of vcgencmd) would cost nothing in fidelity.
- **UPS read is ~29 individual I2C byte transactions per poll**, using paired
  `read_byte_data` calls where `read_i2c_block_data` would do it in a few. Worse
  than slow: each 16-bit value is read low-then-high non-atomically, so a value
  rolling across a byte boundary between the two reads can tear (a battery-current
  reading off by 256 mA). The `raw` register map is also assembled on every poll
  and only ever consumed by a diagnostic script.
- **Camera start/stop runs synchronously on the event loop.** `ensure_started` →
  `picamera2` configure/start can take the better part of a second, during which
  every handler — including `/health` — stalls. Same for `driver.stop()` fired
  from the idle timer. `asyncio.to_thread` around the driver calls would keep the
  service responsive.
- **Each browser tab on `/logs/events` spawns its own `journalctl -f`.** A few
  open tabs means several journal followers tailing eleven units each. One shared
  follower feeding the existing `BroadcastHub` would match the architecture
  already used for action lines.
- **Vision defaults defeat the camera's idle machinery.** With
  `vision.enabled=true` (the default) at 2 Hz, the vision service's snapshot
  fetches restart the camera's 30 s idle timer forever — the camera never idles,
  so all the acquire/release/idle-stop logic only matters when vision is off.
  Possibly intended, but if the goal was camera-off-when-unused, vision's default
  is quietly overriding it.
- **IMU failure churns the range driver.** In `SensorsService._ensure_driver`, if
  the range driver constructs but the IMU factory raises, the whole thing is
  released and rebuilt on the next 1 s tick — so a persistently absent IMU means
  starting and stopping continuous ranging on every ToF sensor once per second,
  indefinitely.

---

## Improvements

- **The web dashboard is a fully unauthenticated control plane on
  `0.0.0.0:8080`.** Anyone on the LAN can restart services, rewrite configs
  (including safety thresholds), trigger a git redeploy, and send voice commands.
  For a home robot that's likely an accepted trade-off, but it deserves to be a
  decision — binding to a tailnet/LAN interface or adding a shared token would be
  cheap.
- **The LiPo cutoff only trusts `robot_motion`-sourced voltage.**
  `_fresh_pack_voltage` ignores the gamepad-teleop battery reading, yet the hub's
  `motor_battery_value` still maintains a full gamepad fallback path. Either
  teleop can still be the sole voltage source in some configuration — in which
  case the cutoff has a blind spot — or that fallback is dead weight in the hub.
  Worth deciding which.
- **Pi shutdown ignores charging state.** Plugging a deeply discharged pack into
  USB-C and booting still triggers `sudo shutdown` if the pack reads ≤ 13.0 V,
  even with `vbus_present`/`charging` true and the pack actively recovering.
  Skipping (or delaying) shutdown while charging would avoid a shutdown loop on a
  recovering battery.
- **`redeploy-robot.sh` can enable-but-never-start a new service.** A newly
  added systemd unit gets `want_restart` via the `systemd/*` case, but the start
  loop only iterates the hardcoded `START_SERVICES` list — a unit missing from
  that list is stopped (if running) and never started. One more list to keep
  aligned by hand.

---

## Suggested priority

| Priority | Item | Why |
|---|---|---|
| 1 | Hub broadcast drain hang (#1) | One bad subscriber silently disables all telemetry *and* the LiPo cutoff |
| 2 | VL53L1X stale last-good (#2) | Safety gate can trust a dead sensor indefinitely |
| 3 | `subscribe()` decode crash (#3) | Frozen dashboards with zero diagnostics; trivially fixable |
| 4 | Pi shutdown single attempt (#5) | The UPS service's whole purpose fails open |
| 5 | Redeploy failed-deploy baseline (#7) | Services silently run stale code after any red test run |
| 6 | Redeploy dependency map (#8) + TUI `LOG_COMMAND` (#6) | Both are "lists that drifted"; cheap to fix together |
| 7 | Hub 64 KB publisher limit (#4) | Latent, but failure mode is confusing telemetry vanishing |
| 8 | vcgencmd subprocess churn | Easy Pi CPU win in the always-on service |
