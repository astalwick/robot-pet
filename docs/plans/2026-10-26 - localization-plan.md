# Localization that just works (2026-10-26)

Arlen: "Come up with a clean plan, not just hacks on top of hacks, as to how we
go from where we are right now to a robot that can reliably localize. My robot
vac can do it. We have better hardware than that thing. I shouldn't need to
command a localize, it should just do it. If I drag it somewhere mid nav, it
should figure it out."

Follows [2026-10-26 - always-localizing.md](2026-10-26%20-%20always-localizing.md)
and the floor results in
[2026-10-26 - floor-test-plan.md](2026-10-26%20-%20floor-test-plan.md).

## TL;DR

Our localizer has one real defect, and everything else follows from it: the
scan fit score cannot tell a right pose from a wrong one. It asks only "does
each beam end near something black on the map", and in a house of walls at
sensible distances a wrong room scores 0.69 and a pose a metre off scores
0.55. Everything built on top (the 0.4/0.6 bars, the spin, "localized yes")
inherits that blindness. AMCL is not the problem; it is a fine tracker when
it is handed a true pose and told when to stop trusting itself.

The fix is the structure every working system in the literature has, and
that we are missing: a **tracker** (AMCL, kept) plus a **search** (match one
full 360 degree scan against the whole map, every saved map, by brute-force
correlation, no spinning) plus a **judge** (a strict score that counts beams
passing *through* mapped walls, and a confirmation step over a little motion
before anything is believed). The judge runs all the time; the search runs
whenever the judge is unhappy. One new node, five phases, the first of which
is measuring, not coding.

## Where we are and why it fails

What runs today, in localize mode:

- AMCL tracks the robot's pose on the saved map and publishes `map -> odom`.
- The nav bridge computes `scan_fit` each tick: the share of beams whose end
  point lands within 3 cells (15 cm) of an occupied cell. A boolean
  `localized` flips to lost after 6 ticks under 0.4 and to found after 3
  ticks over 0.6; between those bars nothing changes.
- A handed pose (remembered pose at boot, "AMCL already tracking") sets
  `localized = True` without checking anything.
- Relocalize: AMCL scatters particles over the whole map, the robot spins a
  full turn up to twice, and the first spin whose fit reaches 0.6 wins.

Floor numbers, all from this weekend:

| situation | scan fit |
| --- | --- |
| true pose, various spots | 0.55 to 0.94 |
| right spot, wrong heading | 0.44 to 0.49 |
| a metre off, dashboard said localized | 0.55 |
| completely wrong room, relocalize accepted it | 0.69 |
| standing on the counter | 0.23 |

A score whose "wrong room" value sits inside its "true pose" range cannot be
rescued by moving the bars. The reason it fails is structural: "end point
near a wall" is satisfied by any pose that puts walls at roughly the right
distances in roughly the right directions, which in a rectangular house is
most poses. It never asks the question that actually separates right from
wrong, "does this beam travel through a wall the map says is solid?" A right
pose almost never does that; a wrong pose does it constantly.

Three smaller defects ride along:

- `localized` is a flag set by whoever last touched it, not a judgement.
- The relocalize search tool is AMCL's particle cloud. 5000 particles spread
  over 40 square metres at every heading is about one particle per 8 cm
  square at a 1-in-360 chance of the right heading. MCL is a tracker; asking
  it to search is why it needs spins and still lands in the wrong room.
- Nothing watches the robot while it is carried, dragged or teleoperated in
  a way that acts: AMCL's recovery sprinkle (alpha slow/fast) only helps
  once the robot drives for a while, and the dashboard keeps saying yes.

## How people actually do this

Short version: every system that relocalizes reliably has the same three
parts. A tracker that follows the robot when its pose is roughly right. A
global search that finds candidates from a single scan without moving. A
confirmation step that refuses to believe a candidate until it has survived
a little motion or clearly beats every alternative. The research is below;
the parts are the pattern.

**Trackers.** Nav2's AMCL is still the default localizer for a prior map
([Nav2 AMCL docs](https://docs.nav2.org/jazzy/configuration_and_development/configuration_guide/others/configuring_amcl/);
a [2026 vehicle paper](https://arxiv.org/pdf/2604.24242) kept it). Beluga
AMCL is a modern drop-in reimplementation with the same parameters, demoed on
a Raspberry Pi 4B ([Beluga](https://github.com/Ekumen-OS/beluga),
[ROSCon 2024 talk](https://roscon.ros.org/2024/talks/Beluga_AMCL-_A_modern_Monte_Carlo_Localization_implementation_for_ROS.pdf));
there is no Lyrical apt package for it (checked on the robot today).
slam_toolbox's localization mode keeps a rolling buffer of scans in a pose
graph on top of a serialized map; it is reported more precise than AMCL but
needs a serialized pose graph (not our PGM), "quite a bit of tuning", and has
no global search: you give it a pose, the dock, or a node id
([slam_toolbox README](https://github.com/SteveMacenski/slam_toolbox),
[ROS Answers](https://answers.ros.org/question/365472/)). Cartographer's
pure localization does carry a global search, but Lyrical ships only its
message package and the project is in maintenance; not a path for us
([Cartographer tuning](https://google-cartographer-ros.readthedocs.io/en/latest/tuning.html)).

**Global search from one scan.** The method is correlative scan matching:
score the scan at every candidate pose on a grid of positions and headings
and take the best. Olson made it real time with a coarse-to-fine two level
search ([Olson 2009, ICRA](https://april.eecs.umich.edu/pdfs/olson2015scanmatch.pdf)
is the 2015 follow-up, M3RSM, which returns the exact brute-force answer ten
times faster). Hess et al. made it fast enough for Cartographer's loop
closure with branch-and-bound over precomputed max grids; the score is the
sum of map probabilities at the scan's end points, the window is the whole
map for global localization, and candidates below a minimum score (0.55 for
local constraints, 0.6 for global) are thrown out
([Hess 2016, ICRA](https://research.google/pubs/pub45466)). The closest
thing to what we need already exists as a reference design: prism_loc's 2D
backend (ROS 2 Humble, C++, v0.1 in 2026-07) runs branch-and-bound over the
whole map, keeps the top 8 distinct modes, re-matches them over 9
odometry-chained scans with a motion gate, and commits only when one
hypothesis holds 90 percent of the posterior, else it reports "ambiguous"
and keeps going. 97 of 100 kidnapped-robot queries succeeded, 38 ms per
full-map search ([prism_loc](https://github.com/kjungmo/prism_loc),
[its parameters](https://github.com/kjungmo/prism_loc/blob/main/PARAMS.md)).
Too new and Humble-only to depend on, right to copy the shape of.

**Knowing you are lost.** The classic fixes for the kidnapped robot problem
are Sensor Resetting Localization (replace particles with ones drawn from
the sensor when the average weight drops;
[Lenser and Veloso](https://www.scitepress.org/Papers/2006/12158/12158.pdf)
summarises it), Mixture MCL ([Thrun et al.](https://www2.cs.sfu.ca/~vaughan/teaching/415/papers/thrun_robust.pdf))
and Augmented MCL, which is what AMCL's `recovery_alpha_*` is. All of them
need a trustworthy "how well does the scan fit here" signal, and the
literature is blunt that overlap-style metrics (ours) are fragile
([Reliable-loc survey](https://arxiv.org/pdf/2411.07815)). Isaac's localizer
declares failure from particle spread over several consecutive frames and
warns about false alarms ([NVIDIA Isaac](https://docs.nvidia.com/isaac/doc/extensions/localization/doc/index.html)).
The discriminating signal is the beam model's: compare the range the map
predicts with the range the lidar measured. A measured range longer than
the map allows means the beam went through a wall, which a right pose
cannot do.

**Robot vacuums.** Public technical detail is thin. Dreame says a new floor
needs its own map and the robot should start on the floor the map belongs
to; the X50 "recognises which floor it is on using landmark detection"
([Dreame multi-floor guide](https://www.dreametech.com/blogs/blog/multi-floor-mapping-robot-vacuum),
[Dreame forum](https://forum.dreametech.com/forum.php?mod=viewthread&tid=1261)).
Valetudo's multi-floor setup is manual map loading, and Dreame robots only
save a new map after returning to the dock
([Home Assistant thread](https://community.home-assistant.io/t/multi-floor-vacuuming-using-valetudo-and-dreame-robots/933706)).
The three things vacuums lean on are visible from that: the dock is a known
anchor pose, lift and wheel-drop sensors tell them they were carried, and a
360 degree scan is matched against each stored map on wake. Nothing there is
beyond us; our lidar is the same class as theirs.

**Maps that learn.** slam_toolbox's "true lifelong" mode (removing nodes as
well as adding) is "highly experimental" and node decay is not implemented
([README](https://github.com/SteveMacenski/slam_toolbox)). The research line
is Dynamic Pose Graph SLAM
([Walcott-Bryant et al.](https://people.csail.mit.edu/kaess/pub/WalcottBryant12iros.pdf))
and submap-replacement frameworks where newer submaps supersede stale ones
([Zhao et al. 2021](https://arxiv.org/pdf/2111.10946)); a BIM-localization
paper notes furniture discrepancies are exactly what hurts AMCL
([arXiv 2308.05443](https://arxiv.org/abs/2308.05443v1)). For a one-floor
house the simple version is enough: a slow, confidence-gated occupancy
update of a copy of the map, which is what a vacuum effectively does when it
"adjusts" after you move a sofa.

## Requirements

Arlen's, in his order:

1. Localizes without spinning in circles.
2. Keeps localizing while navigating or being driven, unless it is already
   confidently right.
3. Detects which map it is on.
4. Accurate above all: never says localized at a 55 percent match.

Later: learns changes to the map; knows when it is on no map at all.

Mine, added:

5. **Honest states, not a flag.** The robot reports one of `tracking`,
   `checking`, `uncertain`, `searching`, `ambiguous`, `lost`, `off_map`,
   `carried`, and which map. Every state is earned by evidence; none is set
   as a side effect of a button or a restart.
6. **Right beats fast.** Committing to a wrong pose is worse than taking ten
   more seconds. A hypothesis is believed only after confirmation.
7. **Safety follows the state.** Trips start only in `tracking`. A drop out
   of `tracking` mid-trip stops the trip. The barrier clearance gate reads
   the same state.
8. **Measured first.** Every threshold comes from recorded floor data,
   replayed offline, with the true answer labelled. No bar is picked by
   feel again.
9. **Fits the Pi 5.** Whole-map search under one second, the judge under
   ten milliseconds per tick, both in numpy (scipy is not on the robot).
10. **Visible.** The dashboard shows the state, the strict score, the map,
    and during a search the candidates, so a wrong belief is seen before it
    is driven on.
11. **Minimal surface.** One new node and one pure-function module with
    tests. AMCL and Nav2 configuration stay as they are except for the seed.

## The design

Three parts; the first exists.

### 1. Tracker: AMCL, as now

AMCL keeps publishing `map -> odom` so Nav2 is never without a frame. It is
re-seeded by us (an `initialpose` with a tight covariance) whenever the
search commits a pose, and we stop reading its covariance as truth. Beluga
AMCL stays a possible swap if AMCL's behaviour ever becomes the bottleneck;
nothing in this plan depends on which one runs.

### 2. Judge: a strict score and a state machine

The score replaces `scan_fit`. For each of about 360 beams of the latest
scan, from the pose TF gives the lidar, ray-cast across the map to the first
occupied cell and compare that predicted range with the measured one:

- **agree**: within 15 cm of the predicted range (or both beyond range).
- **short**: measured well shorter than predicted. Something unmapped is in
  the way: a person, a chair that moved, the counter. Tolerated; this is
  what a changing house looks like.
- **through**: measured well longer than predicted. The beam went through a
  mapped wall. A right pose cannot produce these except at map errors
  (glass, open doors). This is the kill signal.

Two numbers come out: `agree` share and `through` share. A right pose has
high agree and near-zero through; a wrong room has middling agree and lots
of through, which is exactly the case today's score cannot see. Ray casting
360 beams over at most 240 steps of 5 cm is one vectorised numpy step
array, well under 10 ms on the Pi.

The state machine runs every tick, in localize mode, and owns the only
`localized`-like word anyone else reads:

- `tracking`: agree high and through low for a few consecutive ticks since
  the last commit. The only state that permits a trip.
- `checking`: a pose was just handed over (boot, restart, AMCL seed) and has
  not yet earned `tracking`. Replaces today's unconditional yes.
- `uncertain`: the score has degraded for a few ticks, or the robot was
  dragged (odometry says still, scan says the world moved), or AMCL's
  estimate and our search disagree. A trip in progress is stopped. A
  background search starts.
- `searching`, `ambiguous`, `lost`: the search is running, has several
  candidates it cannot separate, or has none. See part 3.
- `carried`: both cliff sensors say no floor, or the IMU sees a lift. Exits
  to `searching` when set down.
- `off_map`: the search found no acceptable candidate on any saved map and
  the robot has since moved enough that it should have.

Thresholds and tick counts are set in phase 1 from recorded data, not
guessed. The bars are stored in `robot_nav.py` next to the pure functions,
with the recorded evidence noted in the comment.

### 3. Search: whole map, one scan, no spin

A RPLidar scan already covers 360 degrees. Spinning the robot adds no
information; it only gave AMCL's particles time to collapse. The search
instead scores one scan at every pose on every saved map:

- Precompute per map a soft lookup grid (distance-to-nearest-wall turned
  into a score, like a likelihood field) and its FFT. Done once at map load.
- For each of 72 headings at 5 degrees, rotate the scan's end points into a
  hit image and correlate it with the lookup grid by FFT. The result is the
  score at every translation for that heading, at once. 72 correlations on a
  256 by 256 grid took 0.17 s on the robot today, so the whole coarse search
  over our map takes under a quarter of a second; three maps under a second.
- Take the top handful of distinct peaks (merge peaks within a metre and 20
  degrees), refine each at 1 degree and one cell, then run the strict judge
  on each. Candidates whose `through` share is high are discarded here, the
  step today's relocalize lacks.
- **Confirm before believing.** If one candidate clearly beats the rest it
  is committed at once: seed AMCL, enter `checking`, then `tracking` as the
  judge agrees over the next ticks. If two or more remain close the state is
  `ambiguous` and the search re-matches the survivors as the robot moves;
  each odometry-chained scan either strengthens or kills a candidate (the
  prism_loc pattern). The motion is whatever is already happening: a
  teleop drive or, when standing still, a short nudge of about 30 cm or 30
  degrees that the search asks the motion node for. Not two spins.
- **Which map.** The search runs on every saved map and the best confirmed
  candidate names the map. Switching maps is what it is today (restart
  map_server and AMCL on the new one) but decided by evidence, not by trying
  maps in turn.
- **Not on a map.** No candidate survives the judge on any map, the robot
  has moved enough for the confirmation step to have had its chance, and the
  judge still fails: `off_map`. The dashboard says so and offers mapping.

Where it lives: a small `localizer` node of its own, because the search can
take up to a second and the nav bridge's half-second tick must not stall.
The bridge keeps everything it does today except the spin and the fit; it
reads the localizer's state from the hub and asks it for a search where it
used to spin. All the maths is pure numpy functions in `robot_nav.py` (or a
sibling module if it outgrows the file) with tests on synthetic and recorded
scans.

### What this gives Arlen's four points

1. No spin: one scan, whole map, candidates in a quarter of a second,
   confirmed by ordinary motion or a small nudge.
2. Continuous: the judge runs every tick in every mode of driving; a
   degraded score or a drag starts a search without anyone pressing a
   button. Above the `tracking` bar nothing is disturbed.
3. Which map: the search scores all maps and picks the one that survives.
4. Accurate: nothing is called `tracking` without a high agree share, a near
   zero through share, and a confirmation over a few ticks. A 55 percent
   match of any kind is `uncertain` and says so.

## Alternatives considered

- **Move the bars, or weight AMCL's covariance in.** No. Wrong room scored
  inside the true range; no bar fixes a score that does not measure the
  right thing.
- **Replace AMCL with slam_toolbox localization mode.** More precise
  tracking, possibly, but no global search, a re-map into pose graphs, heavy
  tuning, and it would still need parts 2 and 3. Maybe later as the tracker.
- **Cartographer pure localization.** Has the global search built in, but
  not packaged for Lyrical beyond messages, Lua configuration, maintenance
  mode. No.
- **Adopt prism_loc.** Right shape, but Humble, C++, three months old. We
  copy its verification pattern and keep our stack.
- **Keep spinning but spin smarter.** Spinning is a workaround for a search
  tool that is not a search tool. Removing it is the point.
- **Write our own particle filter.** Not needed; the tracker is the part
  that works.

## Phases

Each phase ends with something Arlen can see on the floor or in a replay.
Code lands only from phase 1; phase 0 is data.

### Phase 0: measure (one evening)

- The bridge gains a `record` command that writes, each tick, the latest
  scan, the lidar's map pose from TF, odometry, AMCL covariance and the hub
  state to a JSON lines file under `~/.config/robot-pet/recordings/`.
- A script `tools/localization_replay.py` reads a recording and a map and
  prints, per tick, today's `scan_fit`, the new `agree` and `through`
  shares, and the search's top candidates, so scores can be compared without
  the robot.
- Recordings to collect, each labelled with the truth: true pose at ten
  spots incl. the kitchen and living room places; right spot wrong heading;
  a metre off; the wrong room pose from today; on the counter; carried
  across the house; a gamepad drive around the whole floor at the true pose.
- Exit: a table in this document of the three scores for every labelled
  situation. Phase 1's bars are read off that table, with margin.

### Phase 1: the judge (one to two evenings)

- `robot_nav.beam_check(...)`: the ray-cast score, pure numpy, tests on
  synthetic maps and on phase 0 recordings.
- The state machine in the new localizer node; the bridge and dashboard
  read its state; `localized` becomes "state is tracking". Handed poses
  enter `checking`. A trip is only accepted in `tracking`; a drop out of it
  cancels the trip (the gate already exists, it just reads a better input).
- Dashboard: state word, agree and through shares, map name.
- Exit, on the floor: a metre off and the wrong room are never shown as
  `tracking`; a correct pose reaches `tracking` within three ticks of a
  seed; a hand-dragged robot drops to `uncertain` within the chosen ticks.
  The relocalize spin still exists in this phase and now has a judge that
  rejects wrong rooms.

### Phase 2: the search (two to three evenings)

- `robot_nav.global_search(...)`: lookup grid, FFT correlation at 72
  headings, peak picking, refinement, judge filter. Tests on recordings:
  every labelled true pose must be the top confirmed candidate; the wrong
  room recording must not produce a confirmed candidate at the wrong room.
- Confirmation over odometry-chained scans with the motion gate; the nudge
  request to robot_motion when standing still and ambiguous.
- Relocalize becomes "search now": spin code deleted, `RELOCALIZE_SPINS`
  gone, Localize button stays as a manual trigger.
- All saved maps are searched; the winner names the map.
- Exit, on the floor: carried to ten spots, set down, no button pressed by
  the end of the phase (phase 3 adds the trigger; here Localize is pressed):
  committed to the right spot with at most one nudge every time, never to a
  wrong room; a search on the counter ends `ambiguous` or `lost`, not
  committed.

### Phase 3: always on (one to two evenings)

- Triggers for a search: `uncertain` for a few ticks, `carried` ending,
  boot with no remembered pose, boot with a remembered pose that fails
  `checking`, drag detection (scan moves while odometry is still).
- `carried` from the cliff sensors and IMU; the lidar stays on while
  carried only if the rail rules allow it (they do not today; the search
  then starts at set-down when the rail comes back).
- `off_map` after the search fails on every map and the robot has moved.
- Nav2 coupling finished: `uncertain` mid-trip stops the trip, re-plans
  after a re-commit if the goal still stands, refuses while `lost`.
- Exit, on the floor: drag the robot mid-trip two metres and turn it: trip
  stops within two ticks, state goes `uncertain` then `searching`, re-finds
  itself, and the dashboard tells the story without a button press. Carry it
  to another room: same. Put it on the counter: `off_map` or `lost`, no
  trip accepted.

### Phase 4: later

- **Map learning.** A `groundfloor.learned` copy of the map, updated only
  while `tracking` with a high agree share: cells the lidar sees through on
  several separate visits fade toward free, cells hit on several visits grow
  toward occupied, a few percent per visit, never in one go. AMCL, the
  search and the costmap read the learned copy; the saved original is never
  touched; the dashboard shows the difference as a coloured overlay and
  offers "accept into map" and "reset". If slam_toolbox's lifelong mode
  matures, it is the alternative.
- **Dock or boot anchor.** A known parking spot gives a free, strong prior
  at boot, the way vacuums use the dock.
- **Tracker swap.** Beluga AMCL if it gets packaged for our distro or AMCL
  misbehaves. Nothing above changes.

## Open questions for Arlen

- The nudge in phase 2: is a short automatic 30 cm move or 30 degree turn
  acceptable when the robot is standing still and ambiguous, or should it
  wait for a gamepad or trip motion only?
- Where the new node lives: a separate `robot-localizer` unit (recommended,
  keeps the bridge tick safe) or inside the bridge as a worker thread.
- Phase 0 wants about an hour of the floor with the rail on; which evening.

## Sources

- [Olson, M3RSM / real-time correlative scan matching](https://april.eecs.umich.edu/pdfs/olson2015scanmatch.pdf)
- [Hess et al. 2016, Real-Time Loop Closure in 2D LIDAR SLAM](https://research.google/pubs/pub45466)
- [prism_loc, 2D MCL with branch-and-bound global relocalization and verification](https://github.com/kjungmo/prism_loc), [parameters](https://github.com/kjungmo/prism_loc/blob/main/PARAMS.md)
- [Nav2 AMCL configuration](https://docs.nav2.org/jazzy/configuration_and_development/configuration_guide/others/configuring_amcl/), [ReinitializeGlobalLocalization BT node](https://docs.nav2.org/configuration/packages/bt-plugins/actions/ReinitializeGlobalLocalization.html)
- [Beluga AMCL](https://github.com/Ekumen-OS/beluga), [ROSCon 2024 slides](https://roscon.ros.org/2024/talks/Beluga_AMCL-_A_modern_Monte_Carlo_Localization_implementation_for_ROS.pdf)
- [slam_toolbox README](https://github.com/SteveMacenski/slam_toolbox), [JOSS paper](https://joss.theoj.org/papers/10.21105/joss.02783.pdf), [ROSCon 2019 slides](https://roscon.ros.org/2019/talks/roscon2019_slamtoolbox.pdf)
- [Cartographer tuning](https://google-cartographer-ros.readthedocs.io/en/latest/tuning.html), [going further](https://google-cartographer-ros.readthedocs.io/en/latest/going_further.html)
- [Thrun et al., Robust Monte Carlo localization (Mixture MCL)](https://www2.cs.sfu.ca/~vaughan/teaching/415/papers/thrun_robust.pdf)
- [Sensor Resetting Localization summary](https://www.scitepress.org/Papers/2006/12158/12158.pdf)
- [Reliable-loc, survey of localization failure detectors](https://arxiv.org/pdf/2411.07815), [Nubert et al., learned localizability](https://arxiv.org/pdf/2203.05698)
- [NVIDIA Isaac localization failure detection](https://docs.nvidia.com/isaac/doc/extensions/localization/doc/index.html)
- [Free-space features for global localization](https://arxiv.org/abs/1908.01863)
- [Dreame multi-floor guide](https://www.dreametech.com/blogs/blog/multi-floor-mapping-robot-vacuum), [Dreame forum](https://forum.dreametech.com/forum.php?mod=viewthread&tid=1261), [Valetudo multi-floor thread](https://community.home-assistant.io/t/multi-floor-vacuuming-using-valetudo-and-dreame-robots/933706)
- [Walcott-Bryant et al., Dynamic Pose Graph SLAM](https://people.csail.mit.edu/kaess/pub/WalcottBryant12iros.pdf), [Zhao et al. 2021, lifelong localization and mapping framework](https://arxiv.org/pdf/2111.10946), [BIM-based 2D lidar localization](https://arxiv.org/abs/2308.05443v1)
- [ROS Answers, slam_toolbox vs AMCL](https://answers.ros.org/question/365472/), [2026 vehicle paper keeping AMCL](https://arxiv.org/pdf/2604.24242)
