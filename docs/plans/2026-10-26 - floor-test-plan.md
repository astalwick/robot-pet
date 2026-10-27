# Floor test plan for the 2026-10-25/26 work

## Results and bugs found (updated as we go)

Fix batch (2026-10-26 evening, after the first floor session), all in one
commit on ros2-nav2:

- Lidar follows "the motor board answers", not the rail flag. robot-motion
  opens the board whenever the hub says the rail is on and keeps it open while
  idle (reading voltage, so a dead board is noticed within about 5 reads); the
  lidar node turns the lidar on when robot-motion reports `roboclaw_ready`.
  Wall switch off now shows "motor board not answering (wall switch?)" in the
  top bar instead of "waiting for roboclaw", and the lidar stays off.
- Stick double-push: the waiting loop polls at 20 Hz instead of 1 Hz, and with
  the rail on the board is already open, so the first push drives.
- Localized rule is scan fit only. Lost = fit under 40% for 3 s; found = fit
  60% or better for 1.5 s. AMCL's covariance is no longer consulted (it sat on
  the threshold and flickered). Relocalize accepts a map when the fit at
  AMCL's best guess reaches 60% after a spin, and hands that guess back as the
  initial pose so the particles gather. Known gap: a wrong heading at the
  right spot scores 44-49%, between the two thresholds, and is not caught.
- Barrier clearances are published whenever there is a map pose, trusted or
  not, so the gamepad stops at a line even while "localized" is no.
- The bridge's own relocalize spin no longer cancels the trip it is for
  (audit item 13).
- nav2.yaml sets `desired_linear_vel` too, which stops the 60 s warning.

Second fix batch (same evening, after redeploying the first and reading the
journals, no floor test in between):

- BUG (regression from batch 1): with the rail on, the motor loop flapped
  "RoboClaw ready, driving, stopped, waiting for roboclaw" once a second.
  Cause: robot-battery republishes the rail state every 1.0 s and the hub calls
  a source stale after 1.0 s, so one hub snapshot in five showed the rail
  stale and robot-motion read that as "rail off" and let the board go. Fix:
  robot-battery republishes every 0.5 s. (The old lidar code had the same
  stale check; it only looked at edges so the flicker never showed.) First
  attempt changed only the dataclass default; the service takes its interval
  from the command line, whose default was still 1.0, so a 20 degree test turn
  still dropped the board twice after finishing. Second attempt points the
  command-line default at the dataclass. Not yet seen working with the board
  powered (power was off by then); check on the next floor run.
- Seen once in that test turn: right after a reconnect the board did not
  answer ReadVersion for 2 s with the rail on. Probably the quick
  close/reopen of the serial port from the flicker above. Watch for it.
- Re-test after batch 2 (later that evening): all eight re-test steps passed
  (lidar off with the switch off, first push drives, no churn while idle,
  lidar off once when the controller goes, lost on carry, Localize finds it,
  barrier stop). Scan fit 81% at rest afterwards.
- Barrier stop felt closer than 20 cm. Likely cause: the bridge publishes the
  barrier distance on its 0.5 s tick, and at stick speed the robot covers up
  to 18 cm between updates; the map pose is also off by 10-20 cm at the best
  of times. Fix candidates: publish faster, or stop earlier. Not done.
- Section 4 (later that evening): checks 15, 16, 17, 18, 19, 20 and 22 all
  pass. Journal shows the trips, "cancelled: gamepad" and "cancelled:
  requested" as expected, no cancellation we did not cause.
- Check 21, voice "stop": FAILED as predicted (audit item 6). Trip started at
  99314.9; voice went straight into check_navigation(wait 60 s); the stop tool
  did not fire until 99330.3, about 15 s of Arlen saying "stop". It then fired
  three times in 4 s. The kitchen trip at 99338.7 and the "wait no, living
  room" swap 1.7 s later were both asked for and worked well. Root cause of
  the slow stop is audit cluster A: the conversation owns the body, so a tool
  that waits 60 s deafens the robot. Also seen once: "elevenlabs tts send
  failed; retrying".
- Section 7, check 29 (restart robot-nav with a remembered pose): no spin at
  all; the bridge re-sends the remembered pose and localized stays yes. The
  spin only happens with no remembered pose (boot). Audit item 23 (double
  spin) does not arise. Pass.
- Section 7, check 30 (restart robot-nav-bridge 4 s into a trip to the
  kitchen): BUG, safety. The bridge died at 99848.5 but Nav2 kept the trip:
  it drove on, hit "collision ahead", ran its own spin recovery (90 degrees),
  a 5 s wait, then a backup, and only gave up at 99866.9 with "controller
  patience exceeded", 18 s after the bridge was gone. The new bridge started
  with goal None, so dashboard Cancel would have done nothing; only the
  gamepad could have stopped it. Fix: the new bridge cancels any Nav2 goal
  it finds running at startup (covers a crash too), and the old one cancels
  its goal on the way out. Longer term robot-motion should lock Nav2 out when
  the bridge's telemetry goes stale (audit cluster C).
- Same test, second BUG: the restarted bridge re-sent the remembered pose to
  an AMCL that had never stopped and was tracking fine, which reset the
  particle cloud mid-drive (AMCL also warned "failed to transform initial
  pose in time"). The robot ended lost: localized no, scan fit 0.32. Fix:
  only send the remembered pose when AMCL itself has just come up (no AMCL
  pose seen yet), not on a bridge-only restart.
- Third fix batch (commit 584be58, deployed 2026-10-26 evening), both check 30
  bugs: (1) the new bridge sends Nav2 one "cancel every goal" request as soon
  as Nav2 answers, so a trip orphaned by a bridge crash or restart stops
  within the restart gap (3.5 s from stop to the new bridge's first tick, was
  18 s driving to its own failure). The exit-time cancel was dropped: rclpy
  has already shut the node down when the stop signal lands, so nothing can
  be sent then; the startup cancel covers both cases. (2) When the first AMCL
  poll finds it active and giving a map pose, with a remembered pose on this
  map, the bridge keeps AMCL's pose and marks localized instead of re-sending
  the old one. A fresh AMCL gives no map pose until it gets one, so boot and
  a robot-nav restart (check 29) still behave as before.
  Floor results (two runs, bridge restarted 4 s into a kitchen trip):
  run 1 (584be58): Nav2 logged "Client requested to cancel the goal" 4.0 s
  after the restart command (102924.4 to 102928.4); fix 1 PASS. Fix 2 did not
  trigger: the first AMCL poll came 0.5 s after start, before any map pose had
  reached the new bridge over TF, so it re-sent the remembered pose as before.
  Not harmless: the dashboard said localized yes at fit 0.55, but Arlen found
  the robot about a metre from where the map showed it (0.92, 1.06). The
  handed pose sets localized yes unconditionally and 0.55 sits in the dead
  band between the 0.4 lost and 0.6 found bars, so nothing ever re-checked
  it. BUG, same hole as the wrong-heading case in section 3. Fix candidates:
  show "checking" after a handed pose until the fit confirms it (three ticks
  above the found bar, as a relocalize does); make the fit score stricter
  than "a beam within 3 cells of any occupied cell" so a metre off scores
  clearly worse than the real spot; then revisit the bars. Run 2 below kept
  AMCL's pose, which was probably still this wrong one.
  Commit d3b2d50 waits up to 3 s for AMCL's pose before deciding it is fresh.
  Run 2 (d3b2d50): cancel 3.8 s after the restart command, then "AMCL already
  tracking on groundfloor: keeping its pose" 1 s after start, no
  initialPoseReceived in AMCL's log, localized stayed yes, fit 0.52 to 0.57
  before and after. Both PASS. The redeploy's own bridge restart while
  standing still also took the new path. Side notes: the restart gap is now
  the whole exposure (about 3.8 s of Nav2 driving alone); audit cluster C
  (robot-motion locks Nav2 out when the bridge's telemetry goes stale) would
  close that. Scan fit along this trip and at its end was 0.52 to 0.57, below
  the 0.6 found bar, so localized here rests on the rule's stickiness, more
  evidence for the open "60% may be too strict" item. Before this session's
  tests the motor board ignored the rail for three attempts with the switch
  on (GPIO 24 confirmed high); it came back on its own, Arlen suspects a loose
  GPIO lead.
- Shelved idea (Arlen's): replace the spin with a background search of the
  whole map using the current scan (our scan fit score at every free cell and
  heading), acting only on a clearly better pose that wins twice while moving.
  Would make Localize and the restart spin unnecessary. Not started.
- BUG (regression from batch 1): the lidar flapped off/on/off when the rail
  went off, because the motor loop published one last "roboclaw_ready yes"
  on its way out. Fix: that publish is gone; the waiting loop says "not
  answering" right away.
- The redeploy's own relocalize spin on the floor scored fit 0.34 then 0.51
  and gave up ("no saved map converged"), so 60% found may be too strict for
  AMCL's first guess after a global spread. Open; needs more floor numbers.

- Section 0 (2026-10-26 18:07): checks 1 and 2 pass. Check 3 FAILED: the
  lidar spun up when the controller paired with the wall switch off. The lidar
  is USB powered, so the switch never cuts it; the software follows the rail
  flag, not the motor board. Bug, see audit item 18. Fix after the floor tests:
  lidar and rail display follow "motor board answering", battery reports
  `roboclaw_silent`.
- Also seen: lost detection fired on the counter (fit 24%, localized no), as
  designed. Voice panel showed activity STALE with a session open; watch it.
- Section 1, step 4 (18:15): BUG. Robot carried from the counter to the
  floor said "localized: AMCL converged and the scan fits (scan fit 0.44)",
  then 0.49, at the old pose (1.20, 0.58, -83°). It was not there. A fit of
  0.4 at a wrong place passes the SCAN_FIT_FOUND threshold, and once AMCL's
  covariance is tight nothing else is checked. Fix candidates: after "lost",
  stay lost until a relocalize succeeds (do not flip back on fit alone); raise
  SCAN_FIT_FOUND once we know the fit at a right place; weight the fit by how
  many beams are long enough to see a wall. Need: fit at the true position for
  comparison.
- Step 4 cont. (18:19): stick push worked (RoboClaw ready, driving), so the
  switch and board are fine. Dot was roughly right, heading completely wrong.
  Localize: two spins (7.8 s and 8.2 s, 353° and 357° measured), AMCL "not
  converged" after both, "no saved map converged". But AMCL's pose moved to
  (1.28, 0.19, 127°) with scan fit 86-87%, and the dashboard says localized
  NO. Scan fit numbers so far: counter 23%, wrong heading at right spot
  44-49%, after Localize 86-87%. So the rule fails both ways: 0.4 is too low
  to count as found, and after a global search two spins in place never make
  AMCL's covariance tight, so a real 87% match is refused. Fix: after a
  relocalize spin, accept a strong fit (about 0.7+) at AMCL's mean pose and
  set the initial pose there with a tight variance so the particles collapse;
  raise SCAN_FIT_FOUND to about 0.7 and SCAN_FIT_LOST to about 0.4 pending the
  section 2 numbers; once lost, do not flip back to found on fit alone.
- Sections 2/3/5/6 (18:30): scan fit at the true pose ranges 55-90% around
  the ground floor (wrong heading scored 44-49%, so the gap is narrow). Dot
  follows while driving but "localized" stays no: AMCL covariance sits right
  at the thresholds (x 0.29, y 0.13, yaw 0.32 vs limits 0.25/0.2) with 5000
  particles and recovery on, and flickers (localized 95419, lost 1.5 s later).
- BUG: barrier gate was off the whole test. publish_barrier_clearance returns
  early unless localized (nav_node.py:647), so with localized flapping the
  robot drove over the line. Fix: publish whenever a map pose exists.
- Not a bug but the test cannot show it: lidar stop distance is 150 mm, the
  ToF forward stop is 250 mm (sensors.json), so the ToFs always trip first on
  the front. Only backing up exercises the lidar gate.
- BUG: every stick move has to be done twice. The motor loop releases the
  board 0.25 s after the sticks go idle and drops back to the waiting loop,
  which polls the sticks once per second (retry_interval 1.0,
  robot_motion.py:349-351). A push shorter than that is missed. Fix: poll fast
  while the rail is on; only back off 1 s after a failed connect.
- Lidar stops within 5 s of the controller going off: pass.
- Minor: Nav2 controller_server warns every 60 s "parameter
  'FollowPath.desired_linear_vel' is not initialized" (left over from the
  4a63c67 rename). Harmless, fix in nav2.yaml.
- Also: no automatic spin on switch-on. Correct: the spin only happens after
  a boot or a nav restart. The test plan expected one; it was wrong.
- Also: with an idle controller the top bar says WAITING FOR ROBOCLAW and the
  encoder says READ FAIL until a stick moves, because robot-motion only opens
  the board on a move request. By design, but confusing (audit item 20).

Covers: lidar follows the rail, lidar in the safety gate, maps/places/relocalize,
Nav2 goals (voice, dashboard, wheels lock), virtual barriers (Nav2 keepout and
gamepad stop), dashboard Camera/Map switch, the idle-controller fix, and
always-localizing (scan fit, lost detection, relocalize before a goal).

Robot state at `3c78d00`: one map `groundfloor`, places `kitchen` and
`living room`, two barrier lines already drawn.

Known traps while testing (from the UX audit, not yet fixed):

- Saying "stop" does not stop the robot. Keep the controller in hand; a stick
  nudge cancels a trip and stops the wheels. Dashboard Cancel also works.
- Any bare click on the dashboard map sends the robot there.
- A voice trip asked while the robot is lost will spin and then cancel itself
  (audit item 13). Only ask for voice trips while the Map card says localized
  yes.
- Do not redeploy while the rail is on or anything is driving: the test suite
  flips the rail during redeploy.
- Booting or restarting robot-nav makes the robot spin two full turns on its
  own to find itself. Give it room.

Watch the journals on a second screen:

```
ssh pi@robot-pi.local 'journalctl -u robot-nav-bridge -u robot-motion -u robot-voice -u robot-battery -f -o short-monotonic'
```

Nav status any time:

```
ssh pi@robot-pi.local 'curl -s -X POST localhost:8080/nav/command -H "Content-Type: application/json" -d "{\"cmd\":\"nav_status\"}"'
```

## 0. On the counter, wall switch off

1. Dashboard loads; Camera/Map switch flips the box; the Map card spans both
   columns; nav rows show mode localize, map groundfloor, scan fit `--`,
   goal none. "localized yes" here is just the remembered pose, not proof.
2. Pair the controller: Pilot Inputs show sticks and buttons moving.
3. Lidar is not spinning (rail is off).

## 1. Power and lidar, on the floor

4. Wall switch on, controller paired: rail on, lidar spins up within a few
   seconds, journal shows Nav2 coming up, then the automatic relocalize spin.
   Robot should end with localized yes and the dot in roughly the right place.
5. Unpair the controller (hold the button or power it off): rail off after the
   hold, lidar stops within about 5 s, journal shows Nav2 paused. The lidar
   must never be left spinning with the rail off.

## 2. Scan fit numbers (the calibration that matters most)

Thresholds are guesses: under 25% for 2 s means lost, 40% or better means
found. Record the Map card's scan fit % standing still in:

6. the middle of the living room
7. half a metre from a plain wall, facing it
8. a doorway
9. facing the most cluttered area (chairs, people)
10. the kitchen

If normal spots sit under 40%, the thresholds in `src/robot_nav.py` need to
drop. If any spot sits under 25% for more than 2 s while the pose is right,
the robot will wrongly declare itself lost there.

## 3. Localized rule and lost detection

11. Drive with the gamepad around the living room for a minute. Localized
    should stay yes and the dot should follow. Note any flicker to no.
12. Pick the robot up, carry it to the kitchen, set it down. Within about 2 s
    the Map card should flip to localized no and the journal should say the
    pose was dropped.
13. Press Localize. Up to two spins. Expect localized yes, dot in the kitchen,
    heading matching reality. Time it.
14. Repeat 12, but instead of Localize just drive with the gamepad for 15 to
    20 s. AMCL's recovery is on; see whether the pose heals on its own.

## 4. Nav2 trips

15. Dashboard: with the controller paired but untouched, click a clear spot
    about 2 m away. Goal row goes navigating, wheels move, row goes arrived.
    This is the key check for the last commit: before it, a paired idle
    controller silently muted Nav2.
16. Watch the path near walls: planner should keep about half a body width
    away.
17. Mid-trip, nudge a stick: goal row says cancelled (stopped), wheels stop,
    nothing resumes when released.
18. Mid-trip, dashboard Cancel: row says cancelled (requested).
19. Voice, while localized yes: "go to the kitchen". Expect accepted, drive,
    arrival, and the robot says so. Note how long the conversation is dead
    (audit items 2 to 5); that is expected for now.
20. Voice: "go to the living room" from the kitchen. Same.
21. Voice "stop" mid-trip: expect it to NOT stop (audit item 6). Have the
    controller ready.
22. Save a new place with the dashboard, then go there by voice from across
    the room.

## 5. Barriers

23. Draw a line across the kitchen doorway on the map. Ask for a trip into the
    kitchen: Nav2 should either route around or report failure, never cross.
24. Drive the gamepad straight at the line: the wheels stop about 20 cm before
    it, journal shows a safety block starting with reason barrier_ahead and
    ending when you back off. Reverse and turning still work.
25. Delete the line; the same drive goes through.
26. Barrier tool is refused in mapping mode (audit item 32) and says nothing;
    just confirm it does nothing rather than crash.

## 6. Lidar safety gate

27. Drive the gamepad at a box or wall: forward stops at the configured lidar
    distance, turning in place still works, backing into something stops too.
28. Drive forward immediately after the rail comes on: the gate is absent for
    the first 2 to 4 s (audit item 27); note whether that matters in practice.

## 7. Restart behaviour

29. `systemctl restart robot-nav` with room around the robot: expect one
    automatic relocalize; note whether it fires twice (audit item 23).
30. `systemctl restart robot-nav-bridge` mid-trip: trip is gone; note what the
    dashboard shows and whether the wheels stopped cleanly.

## What to bring back

- Scan fit % per spot from section 2.
- Time from Localize press to localized yes, and from goal to first wheel
  movement.
- Any "navigation cancelled: stopped" in the journal that you did not cause
  with the controller. That is either audit item 13 or a regression of the
  idle-controller fix.
- Anything that spun or drove without being asked, beyond the known boot spin.
