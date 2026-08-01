# Motion Power Telemetry Deadlock Plan

Goal: stop voice motion intents from power-cycling the RoboClaw rail when no
gamepad is connected.

The bug is a telemetry ownership problem with a hardware timing symptom.
`gamepad_teleop` and `robot_motion` both publish updates under the
`gamepad_teleop` telemetry source. The hub keeps only the latest message per
source, so the two services overwrite each other. `robot-battery` then turns the
motor rail on or off depending on which update arrived last.

Follow `AGENTS.md`: make small direct changes, keep source ownership obvious,
and avoid generic telemetry frameworks or new service layers.

## Desired Behavior

With no gamepad connected:

1. Voice sends a motion intent such as `wiggle`.
2. `robot-motion` accepts the intent and publishes
   `motion_power_requested=True`.
3. `robot-battery` turns the motor rail on and keeps it on long enough for the
   RoboClaw to boot.
4. `robot-motion` connects to RoboClaw, enters the motor loop, runs the intent,
   completes the voice request, and publishes `motion_power_requested=False`.
5. `robot-battery` turns the rail off after the request clears and no gamepad is
   connected.

With a gamepad connected, existing teleop behavior should stay unchanged.

Low-battery cutoff must still win over every other reason to power the rail.

## Important Existing Code

- `src/telemetry/messages.py`
  - `gamepad_teleop_update(...)` hardcodes `"source": "gamepad_teleop"`.
  - `drive_status_message(...)` includes `motion_power_requested` only when the
    caller passes a non-`None` value.
- `src/robot_telemetry.py`
  - `TelemetryHub.latest` stores one update per source.
  - `build_snapshot()` currently reads controller, wheels, motor battery,
    link loop, drive tuning, and drive status from `gamepad_teleop`.
- `src/gamepad_teleop.py`
  - Publishes gamepad status and drive commands.
  - Its `drive_status` does not include `motion_power_requested`.
  - It loads and publishes the real `drive_tuning`.
  - It always publishes unknown motor battery voltage.
- `src/robot_motion.py`
  - Publishes waiting and motor telemetry with `gamepad_teleop_update(...)`.
  - Publishes `motion_power_requested` from `_motion_power_requested()`.
  - It is the only service that reads RoboClaw pack voltage.
  - Ticks intents only in `_run_motor_loop()`, not while waiting for RoboClaw.
- `src/robot_battery.py`
  - Reads gamepad connected state from source `gamepad`.
  - Reads `motion_power_requested` from source `gamepad_teleop`.
  - Immediately turns the rail off when no gamepad or motion power request is
    visible.
- Dashboard and voice consumers read the merged snapshot fields, not raw source
  messages:
  - `snapshot.controller`
  - `snapshot.wheels`
  - `snapshot.motor_battery`
  - `snapshot.link_loop`
  - `snapshot.drive_tuning`
  - `snapshot.drive_status`

## Phase 1 - Give Robot Motion Its Own Telemetry Source

### Why

Two processes must not publish different meanings into the same source slot.
Fixing this is the core bug fix.

### Work

In `src/telemetry/messages.py`:

1. Add a new function named `robot_motion_update(...)`.
2. Give it the same simple payload shape currently used by
   `gamepad_teleop_update(...)`, but set `"source": "robot_motion"`.
3. Keep the signature concrete. A good first version is:

   ```python
   def robot_motion_update(
       wheels: dict[str, Any],
       motor_battery: dict[str, Any],
       now: float | None = None,
       link_loop: dict[str, Any] | None = None,
       drive_status: dict[str, Any] | None = None,
   ) -> dict[str, Any]:
   ```

4. Do not make a generic `source_update(...)` helper unless there are at least
   three duplicated update builders later.

In `src/robot_motion.py`:

1. Import `robot_motion_update` instead of `gamepad_teleop_update`.
2. In `_publish_waiting_telemetry(...)`, publish `robot_motion_update(...)`.
3. In `_publish_telemetry(...)`, publish `robot_motion_update(...)`.
4. Keep the existing payload fields the same, including:
   - `wheels`
   - `motor_battery`
   - `link_loop`
   - `drive_status`
5. Do not publish `drive_tuning` from `robot_motion`. `drive_tuning` is owned
   by `gamepad_teleop`.
6. Do not change intent behavior in this phase.

### Tests

In `tests/test_telemetry_messages.py`:

- Add a test that `robot_motion_update(...)` returns:
  - `"type": "source_update"`
  - `"source": "robot_motion"`
  - the passed `wheels`, `motor_battery`, `link_loop`, and `drive_status`.
  - no `drive_tuning` field.

In `tests/test_robot_motion.py`:

- Update existing published telemetry assertions to expect
  `message["source"] == "robot_motion"`.
- Keep the assertions that `motion_power_requested` and `roboclaw_ready` are
  present in `drive_status`.

### Acceptance

```bash
python3 -m unittest tests.test_telemetry_messages tests.test_robot_motion
```

## Phase 2 - Merge Motion Telemetry Into Snapshots Deliberately

### Why

Consumers should keep reading the same top-level snapshot fields, but the hub
should compose those fields from the right owner.

Gamepad owns controller/input state. Motion owns RoboClaw/motor state.

### Work

In `src/robot_telemetry.py`, update `TelemetryHub.build_snapshot()`:

1. Read both source records:
   - `gamepad = self.latest.get("gamepad_teleop")`
   - `motion = self.latest.get("robot_motion")`
2. Add `"robot_motion": self._source_status(motion, now)` to
   `snapshot["sources"]`.
3. Keep these fields from `gamepad_teleop`:
   - `controller`
   - `drive_tuning`
4. Take these fields from `robot_motion`:
   - `wheels`
   - `motor_battery`
   - `link_loop`
   - `drive_status`
5. Use simple fallback behavior so dashboards are not blank before
   `robot_motion` publishes:
   - For each motion-owned field, use the motion value when present.
   - Otherwise use the old gamepad value.
   - Do not use fallback for `drive_tuning`; always use `gamepad_teleop`.
6. Do not add nested `snapshot["motion"]` or `snapshot["teleop"]` objects in
   this pass. Preserve the existing top-level snapshot shape.

### Tests

In `tests/test_robot_telemetry.py`:

- Add a test where both sources publish:
  - `gamepad_teleop` has controller data and a drive status without
    `motion_power_requested`.
  - `robot_motion` has `drive_status.motion_power_requested=True` and motor
    battery data.
- Assert the built snapshot contains:
  - `sources.robot_motion.stale is False`
  - `controller` from `gamepad_teleop`
  - `drive_tuning` from `gamepad_teleop`
  - `drive_status` from `robot_motion`
  - `motor_battery` from `robot_motion`
- Add a fallback test where only `gamepad_teleop` has published and the old
  top-level fields still appear.

### Acceptance

```bash
python3 -m unittest tests.test_robot_telemetry
```

Then run:

```bash
python3 -m unittest tests.test_telemetry_messages tests.test_robot_motion tests.test_robot_telemetry
```

## Phase 3 - Make Robot Battery Read Motion Power From Robot Motion

### Why

After Phase 2, `motion_power_requested` and `motor_battery.pack_voltage` are
owned by `robot_motion`. Battery should read the owner directly instead of
depending on the merged top-level field's old source.

This also repairs low-battery cutoff. Today pack voltage is flaky for the same
clobber reason as `motion_power_requested`: `robot-motion` is the only service
that reads RoboClaw voltage, while `gamepad-teleop` publishes unknown voltage
and can overwrite the source slot.

### Work

In `src/robot_battery.py`:

1. Change `_motion_power_requested(...)` to check:
   - `sources.robot_motion.stale is False`
   - `snapshot.drive_status.motion_power_requested is True`
2. Keep `_gamepad_connected(...)` reading from source `gamepad`.
3. Keep low-battery cutoff behavior unchanged.
4. Change `_fresh_pack_voltage(...)` to check:
   - `sources.robot_motion.stale is False`
   - `snapshot.motor_battery.pack_voltage` is present
5. Do not accept `gamepad_teleop` as the fresh source for pack voltage.
   `gamepad-teleop` does not read RoboClaw voltage.
6. Keep this logic explicit. Do not add a generic source resolver.

Suggested simple first version:

```python
def _motion_source_live(self, snapshot):
    return ((snapshot.get("sources") or {}).get("robot_motion") or {}).get("stale") is False
```

Only add this helper if it avoids repeating the same source lookup three times
in this file. Otherwise inline it.

### Tests

In `tests/test_robot_battery.py`:

- Update `snapshot(...)` test helper to include `sources.robot_motion`.
- Add or update tests for:
  - Fresh `robot_motion` with `motion_power_requested=True` turns rail on.
  - Stale `robot_motion` with `motion_power_requested=True` does not turn rail
    on.
  - Fresh `robot_motion` pack voltage below cutoff triggers low-battery cutoff
    after the existing debounce.
  - Stale `robot_motion` pack voltage does not trigger low-battery cutoff.
  - Fresh `gamepad_teleop` with pack voltage does not trigger low-battery
    cutoff, because it is not the voltage owner.
  - Fresh `gamepad_teleop` with no motion source does not turn rail on just
    because old drive status fields are present.
  - Connected gamepad still turns rail on even if `robot_motion` is stale.
  - Low-battery cutoff still blocks both gamepad and motion power requests.

### Acceptance

```bash
python3 -m unittest tests.test_robot_battery
```

## Phase 4 - Add A Short Motor Rail Hold After Motion Requests

### Why

The primary fix removes the clobber, but hardware still benefits from a small
settle window. RoboClaw should not lose power because one telemetry frame is
late while it is booting.

### Work

In `src/robot_battery.py`:

1. Add a small config field to `BatteryConfig`:

   ```python
   motion_power_hold_seconds: float = 5.0
   ```

2. Add one runner field:

   ```python
   self.motion_power_hold_until = 0.0
   ```

3. In `_sync_power(...)`:
   - If low-battery cutoff is active, return as today.
   - If gamepad is connected, turn rail on as today.
   - If motion power is requested:
     - set `self.motion_power_hold_until = now + self.config.motion_power_hold_seconds`
     - turn rail on with reason `"motion_power_requested"`
   - Else if `now < self.motion_power_hold_until`, keep the rail on.
   - Else turn the rail off when currently on.
4. Pass `now` into `_sync_power(snapshot, now)` from `_handle_snapshot(...)`.
5. Do not make the hold apply to low-battery cutoff.
6. Do not add background timers. The existing snapshot loop is enough.

### Tests

In `tests/test_robot_battery.py`:

- Add a test that a motion request turns the rail on and a following snapshot
  without the request does not immediately turn it off before the hold expires.
- Add a test that the rail turns off after the hold expires when there is still
  no gamepad and no motion request.
- Add a test that low-battery cutoff still turns the rail off during the hold.

Use a simple fake clock, as existing tests already do.

### Acceptance

```bash
python3 -m unittest tests.test_robot_battery
```

## Phase 5 - Bound Stuck Motion Intents While Waiting For RoboClaw

### Why

If RoboClaw never becomes ready, the voice request should fail and
`motion_power_requested` should eventually clear. A stuck intent should not hold
the motor rail forever.

This is a secondary safety fix. Do it after the telemetry source split is green.

### Work

In `src/robot_motion.py`:

1. Add a small config field to `MotionConfig`:

   ```python
   intent_wait_timeout: float = 8.0
   ```

2. Track when the active/pending intent first requested power while waiting for
   RoboClaw.
3. In `_wait_for_roboclaw(...)`, after `_service_intent_requests(now)`:
   - If `_motion_power_requested()` becomes true for the first time, remember
     `now`.
   - If RoboClaw is still not ready after `intent_wait_timeout`, fail the
     pending intent and clear the executor.
4. Clear both pieces of motion intent state:
   - `self.pending_intent_complete`
   - `self.intent_executor._active`
5. Use the existing pending completion path where possible:
   - `_fail_pending_intent("roboclaw_unavailable")`
6. Then clear the executor with the smallest direct method needed.
7. If the current `MotionIntentExecutor` has no clear/cancel method, add the
   smallest direct method needed in `src/control/motion_intent.py`, for example:

   ```python
   def cancel(self) -> None:
       self._active = None
   ```

   The current field is `_active`, not `active`.
8. Do not tick or simulate the wiggle while RoboClaw is unavailable. The intent
   cannot physically run until the motor loop has a motor.

### Tests

In `tests/test_robot_motion.py`:

- Add a test where:
  - an intent is pending,
  - `motor_factory` keeps returning a motor that does not acknowledge, or raises
    a recoverable connection error,
  - fake time advances past `intent_wait_timeout`,
  - the pending completion receives `{"ok": False, "error": "roboclaw_unavailable"}`,
  - `runner.pending_intent_complete is None`,
  - `runner.intent_executor.is_active()` is `False`,
  - `_motion_power_requested()` becomes `False`.
- Keep the existing test that a motion intent requests power before waiting for
  RoboClaw.

In `tests/test_motion_intent.py`, only add a cancel test if a new cancel method
is added.

### Acceptance

```bash
python3 -m unittest tests.test_robot_motion tests.test_motion_intent
```

## Phase 6 - Dashboard And Voice Availability Checks

### Why

The dashboard and voice inspector currently use `sources.gamepad_teleop` as the
freshness check for drive and battery fields. After the source split, those
fields may be owned by `robot_motion`.

### Work

In `src/voice/assistant.py`:

1. For battery and drive availability, check `sources.robot_motion` first.
2. Fall back to `sources.gamepad_teleop` only for old snapshots or startup.
3. Keep the returned tool shape unchanged.

In `src/web_dashboard_static/telemetry.js`:

1. Add a `motionStale` value from `sources.robot_motion`.
2. Use motion freshness for battery, wheels, link, and drive status displays
   where appropriate.
3. Keep controller/gamepad freshness based on `sources.gamepad_teleop` or
   `sources.gamepad`, depending on the existing display.
4. Do not redesign the dashboard.

In `src/robot_dashboard.py`:

1. Add `robot_motion` source labeling near the current gamepad source display.
2. Use motion freshness for motor-side status if the code checks source
   freshness before rendering those fields.
3. Keep layout changes minimal.

### Tests

In `tests/test_voice_core.py`:

- Add or update snapshot inspection tests so battery and drive are available
  when `sources.robot_motion.stale is False`.
- Keep a fallback test for old snapshots with only `gamepad_teleop`.

In `tests/test_robot_dashboard.py`:

- Update expected source/status rendering if needed.

For browser dashboard JavaScript, run the existing web dashboard tests if they
cover the changed code:

```bash
python3 -m unittest tests.test_robot_web_dashboard
```

### Acceptance

```bash
python3 -m unittest tests.test_voice_core tests.test_robot_dashboard tests.test_robot_web_dashboard
```

## Final Test Pass

Run the focused tests changed by this plan:

```bash
python3 -m unittest tests.test_telemetry_messages tests.test_robot_telemetry tests.test_robot_motion tests.test_motion_intent tests.test_robot_battery tests.test_voice_core tests.test_robot_dashboard tests.test_robot_web_dashboard
```

Then run the broader motion/telemetry/voice set:

```bash
python3 -m unittest tests.test_control tests.test_safety_gate tests.test_telemetry_messages tests.test_robot_telemetry tests.test_robot_motion tests.test_robot_battery tests.test_gamepad_teleop tests.test_voice_core
```

## Pi Smoke Test

After unit tests pass, deploy and test on the robot.

1. Stop the gamepad or leave it disconnected.
2. Restart these services:
   - `robot-telemetry`
   - `robot-motion`
   - `robot-battery`
   - `gamepad-teleop`
   - `robot-voice`
3. Say "wiggle".
4. Expected logs:
   - `robot-motion` publishes `motion_power_requested=True` from source
     `robot_motion`.
   - `robot-battery` logs one rail-on event for `motion_power_requested`.
   - No repeated `motor rail off: idle_no_gamepad` during RoboClaw boot.
   - RoboClaw becomes ready.
   - Wiggle completes or fails cleanly with `roboclaw_unavailable`.
   - If wiggle completes, `motion_power_requested` clears and the rail turns off
     after no gamepad is connected.
5. Reconnect gamepad.
6. Expected logs:
   - `gamepad_connected` keeps the rail on.
   - Teleop still drives normally.

## Do Not Do In This Pass

- Do not replace the telemetry hub with a generic event bus.
- Do not introduce a schema registry or source manager.
- Do not redesign dashboard data shapes.
- Do not tick physical motion intents before RoboClaw is ready.
- Do not make low-battery cutoff depend on the new hold timer.
- Do not add config/dashboard knobs for hold seconds or timeout unless hardware
  testing proves the constants need regular tuning.
