# Always localizing (2026-10-26)

Arlen: "I want this robot to be constantly localizing to some extent. It
should be smart enough to figure out where it is - whether I pick it up, or
drag it around, or what."

Today the robot only finds itself when told to (the Localize button, a boot
with no remembered pose). Carried to another room it keeps believing the old
pose: AMCL grades its particles on a curve, so a cloud that matches nothing
still has a best particle with a tight covariance, and the bridge reads that
covariance as "localized". A navigate_to while not localized is refused.

Follows [2026-10-25 - virtual-barriers.md](2026-10-25%20-%20virtual-barriers.md).

## What gets built

Three pieces, smallest first.

- **Let AMCL heal itself while driving.** `config/nav2.yaml` turns on AMCL's
  recovery (`recovery_alpha_slow: 0.001`, `recovery_alpha_fast: 0.1`, the
  textbook values). When the scans stop matching the map for a while, AMCL
  sprinkles random particles over the free space, so a robot that is driven
  around with the gamepad after being moved finds its real spot again instead
  of tracking a wrong one. Nothing else changes: AMCL still only updates when
  the robot moves.

- **Know when we are lost: a scan-fit score.** Every tick (0.5 s) in localize
  mode the bridge takes the latest lidar scan and the pose TF gives for the
  lidar (`map` -> `laser`), draws each beam's end point onto the saved map and
  counts how many land within two cells (10 cm) of a wall or piece of
  furniture the map knows. The fraction is `scan_fit`, 0..1. A robot standing
  where it thinks it stands scores high; a robot that was carried somewhere
  else scores like random, low.
  - `robot_nav.scan_fit(...)` is the pure math (numpy, tested with synthetic
    ray-cast scans). The bridge keeps the latest `/map` grid and `/scan`
    message and does the TF lookup at the scan's time stamp.
  - The score rides the `nav` hub message as `scan_fit` (None while there is
    no fresh scan, no map pose or no map) and shows as a row on the
    dashboard's Map card, so Arlen can see what "good" and "lost" look like
    on the real floor before the threshold is tuned.
  - One rule for `localized`, evaluated per tick instead of per `/amcl_pose`
    message: lost when AMCL's covariance spreads out, or when the fit has
    been under `SCAN_FIT_LOST` (0.25) for `SCAN_FIT_LOST_TICKS` (4, two
    seconds) in a row; found again when the covariance is tight and the fit,
    if there is one, is at least `SCAN_FIT_FOUND` (0.4). No covariance yet or
    no fit is "no opinion", never a vote for lost: a rail-off robot that was
    just handed a remembered pose stays localized. Being lost deletes the
    remembered pose file, so a restart does not trust it, and cancels a
    goal in progress (`not_localized`).
  - The fit is skipped on ticks where the robot turned more than 0.2 rad
    since the last one: a scan taken while spinning at gamepad speed is
    smeared by several degrees and would read as lost.

- **Fix it before going anywhere.** `navigate_to` no longer refuses with
  `not_localized`. The goal is accepted and waits, as goals already wait for
  the lidar and Nav2; `send_pending_goal` sees the robot is not localized and
  runs a relocalize first (the same spins as the Localize button, on the
  current map first), then sends the goal. The goal's 30 s start clock runs
  from the end of the relocalize. If the relocalize fails the goal fails with
  `not_localized`, as before, only later. A relocalize is tried once per
  goal.

Not built, on purpose:

- **No spin on its own when lost is detected.** The lidar comes on when the
  gamepad connects or a goal is asked for. Spinning uninvited then would
  fight the driver, and tonight's robot sits on a counter. The robot spins
  when asked to localize or asked to go somewhere.
- **No "I was lifted" detector from the cliff sensors.** The scan fit before
  any goal covers the case that matters (carried, then asked to navigate),
  and a cliff-based flag would need its own plumbing from robot-motion.

## Review against how the robot is set up

- `localized` was flipped in three places (`set_initial_pose`, `relocalize`,
  `on_amcl_pose`). The per-tick rule replaces only the `on_amcl_pose` one;
  the relocalize thread still owns the flag while `activity` is set, exactly
  as today, and `set_initial_pose` still sets it True (the remembered pose is
  trusted until a scan says otherwise).
- `update_map_png` threw the grid away after rendering (`latest_map = None`)
  so a changed map would be re-rendered once. The fit needs the grid kept,
  so the bridge now remembers which grid it last rendered instead. In
  mapping mode the grid is slam's live map; the fit is only computed in
  localize mode, where the map is the saved one.
- The scan is in frame `laser`, a fixed child of `base_link` from the URDF
  (`LIDAR_MOUNT`), so TF `map -> laser` exists whenever `map -> base_link`
  does. Looking it up at the scan's own time stamp needs `map -> odom` to
  cover that time: AMCL stamps it `transform_tolerance` (1 s) ahead, and odom
  runs at 20 Hz, so a scan at most half a second old resolves. A lookup that
  fails is "no opinion".
- Thresholds are guesses tonight. The robot is on a counter with the rail
  off: with the lidar switched on briefly through `/lidar/power` the fit at
  the remembered floor pose is a real "lost" sample. A "found" sample needs
  the robot on the floor, so the numbers are constants at the top of the
  bridge, logged on every flip and shown on the dashboard.
- The voice `navigate_to` tool goes through the same socket verb, so it gets
  the relocalize-first behaviour for free. Its description still says "Fails
  if the robot does not know where it is yet"; now it says it will spin to
  find itself first.
- Tests: `scan_fit` with a synthetic room (a square of walls, beams ray-cast
  from the true pose score near 1.0; the same beams from a pose a metre away
  score low; too few valid beams give None), the `localized` rule as a pure
  function in `robot_nav` so the hysteresis is tested without rclpy, the hub
  message's new key, and the nav2.yaml text check for the recovery alphas.
- Deploying tonight is safe: robot-nav and the bridge restart, the pose file
  exists so no relocalize is triggered, and a `turn` intent with the rail off
  would just fail. Movement is not expected to work until the rail is back.
