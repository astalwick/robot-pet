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
before anything is believed) plus a **learner** that keeps the one map true
as the house changes. The judge runs all the time; the search runs whenever
the judge is unhappy. That one question, "do I know where I am, and is there
another spot that makes more sense", covers drags, carries and reboots alike;
nothing detects them separately. One new node, five states, five phases.

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

## The goal

A robot that always knows where it is, without being told. It finds its
position on the map from where it stands, without spinning. It keeps
checking that position while it drives or is driven and fixes it when it is
wrong. It knows which saved map it is on. It never calls itself localized
when it is not. Carried, dragged mid-trip, rebooted: it works it out on its
own, and the dashboard tells the truth about what it believes. Later, the
map absorbs changes to the house, and the robot knows when it is on no map
at all. Done cleanly, as one design, not more patches on the current score.

One principle runs through the design: **contradiction triggers, prior
guides, motion confirms.** A robot that knew where it was and drove into a
featureless corner is not lost; it has little to see. Only evidence that
contradicts where odometry says it should be (beams through mapped walls, a
world that moved while the wheels did not) starts a search, the search
starts from where odometry says the robot should be, and nothing is believed
until a little motion confirms it.

## Requirements

Arlen's, in his order:

1. Localizes without spinning in circles.
2. Keeps localizing while navigating or being driven, unless it is already
   confidently right.
3. Detects which map it is on.
4. Accurate above all: never says localized at a 55 percent match.

Later: learns changes to the map; knows when it is on no map at all.

Agreed with Arlen on 2026-10-27, after asking what would make him say "why
doesn't this work the way I expected":

- **Everyday changes are not errors.** A moved chair, an open door, a person
  walking past, the dog: the robot stays `tracking` through all of them. The
  judge tolerates things that are closer than the map says (something new in
  the way) and only distrusts things that are farther (a beam through a
  mapped wall), with a tolerance read off recordings. Through-beams that
  all land in one small patch, a door's width or less, mean "something
  changed here", not "I am lost": the robot stays `tracking` and the learner
  gets to see it. Only through-beams spread across the scan are a
  contradiction. This is the same tolerance for unexplained beams every
  particle filter carries, made explicit. The learner (design part 4, phase
  4) is what keeps this true over months, not a luxury.
- **The map is audited before any bar is trusted.** A strict judge punishes
  a correct pose wherever the map is wrong: doubled walls from an imperfect
  loop closure, ghosts of people or open doors from mapping day, missing
  glass or dark surfaces. Unknown (grey) cells are "no opinion", never a
  wall. Through-beams at known-true poses that cluster on a few cells mark
  map errors to fix or re-map; through-beams spread everywhere mean the
  judge's tolerance is wrong. Re-map with care if needed: doors in their
  usual state, nobody walking, slow drive with loop closures.
- **Numbers to hold it to.** Position within 10 cm and heading within 5
  degrees while `tracking`. A clear candidate within 5 s of being set down
  where the spot is unambiguous, `tracking` within 20 s including the one
  nudge that confirms it. Zero false `tracking` over twenty
  carried-and-set-down trials; one miss is a bug, not a statistic. Truth
  comes from a few taped crosses on the floor whose map coordinates are
  measured once; a robot set on a cross has a known pose, and "within 10 cm"
  is checked against that, not against a guess.
- **After a stop mid-trip.** If the robot re-finds itself within 30 s and
  the goal still stands, the trip resumes by itself. If it cannot, it says so
  out loud through the voice and gives the trip up.
- **Lidar off means belief stale.** Continuous localization exists only
  while the lidar spins. When it has been off, the state is `stale`: the
  dashboard says so, and the rail coming back re-checks before any trip.
  Nothing detects a carry while the lidar is off; the re-check when it comes
  back is what notices. The lidar power rules themselves live in another plan, with Arlen's rule
  recorded here: a voice interaction delays the lidar going off (one minute
  after the last interaction) but does not start it unless it triggers
  motion; emotes and in-place actions never wait for a map fix.
- **What is allowed while not tracking.** Gamepad driving: yes, always.
  Trips: refused. Barriers live on the map, so barrier protection is
  replaced by the sensors alone (cliff, forward distance, lidar stop) and the
  dashboard says so. Emotes and in-place actions: yes.
- **Not on a map needs motion.** Standing still in an unmapped room the
  honest state is `searching` ("nothing found yet"). `off_map` is declared
  after about 2 m of travel or 60 s of trying with no candidate surviving on
  any map, and the dashboard says it is waiting for motion until then.
- **The voice reports the state.** "Go to the kitchen" while searching gets
  "I'm not sure where I am yet, give me a second", not a refusal or a spin.
  A failed re-find after a stop is announced. (The deafness-during-trips
  item from the UX audit is separate and still open.)
- **Dashboard honesty.** While searching, the map shows the candidate spots,
  not a confident marker at the old pose. Expect the nudge to fire more often
  in hallways and symmetric corners; if that annoys, the prior (below) is
  what reduces it.

Mine, added:

5. **Honest states, not a flag.** The robot reports one of `tracking`,
   `checking`, `searching`, `stale`, `off_map`, and which map; `searching`
   carries a plain-text reason. Every state is earned by evidence; none is
   set as a side effect of a button or a restart. (Arlen cut a longer list:
   "reduce to the ones that actually convey useful information".)
6. **Right beats fast.** Committing to a wrong pose is worse than taking ten
   more seconds. A found pose seeds the tracker at once but is `checking`,
   not `tracking`, until the robot has moved and the judge still agrees.
   Standing still and re-scoring the same scan proves nothing.
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

Four parts; the first exists. The tracker follows, the judge doubts, the
search finds, the learner keeps the map true.

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
  mapped wall. A right pose produces few of these, and the few it does
  produce land together on a handful of cells: a door frame, glass, a door
  that was closed on mapping day, a wall end clipped by a beam at a slight
  heading error. The share at true poses is measured in phase 1, not
  assumed. Through-beams spread across the scan are the kill signal.

Two numbers come out: `agree` share and `through` share. A right pose has
high agree and low through; a wrong room has middling agree and lots of
through, which is exactly the case today's score cannot see. Beams that
reach an unknown cell before any wall stop counting there: they are neither
agree nor through. Ray casting 360 beams over at most 240 steps of 5 cm is
one vectorised numpy step array, well under 10 ms on the Pi.

The two numbers mean different things and are used differently. `through`
is contradiction: the only thing that moves the state toward `searching`,
and only when the through-beams are spread out. Through-beams that all land
in one small patch (a door's width or less) are "something changed here":
shown on the dashboard, handed to the learner, and not a contradiction.
`agree` is confidence: low agree with little through means "little to see
here" (a hallway, a corner, an open room), and the state stays `tracking`
riding on odometry, with the lower confidence shown. This is the "I drove
here from a spot I knew, so this is that corner" rule.

The state machine runs every tick, in localize mode, and owns the only
`localized`-like word anyone else reads:

- `tracking`: agree high and spread-out through low for a few consecutive
  ticks after the robot has moved since the pose was handed over. The only
  state that permits a trip.
- `checking`: a pose was just handed over (boot, restart, a search result)
  and has not yet earned `tracking`. Earned by motion plus agreement, not by
  time. If nothing else moves and a trip is waiting, the nudge supplies the
  motion. Replaces today's unconditional yes.
- `searching`: the spread-out through share has been high for a few ticks,
  or a `checking` pose failed. A trip in progress is stopped. The search
  runs (part 3) and the state carries a reason in plain text: "contradiction",
  "several candidates", "nothing found yet". Drags and carries end up here
  without being detected: the judge simply stops agreeing.
- `stale`: the lidar is off, so nothing has checked the pose since it went
  off. The last pose is kept and shown as stale; the rail coming back enters
  `checking` first.
- `off_map`: the search found no acceptable candidate on any saved map and
  the robot has since moved about 2 m, or tried for 60 s, so it should have.

What each state allows:

| state | trips | gamepad | emotes, in-place actions | barriers |
| --- | --- | --- | --- | --- |
| tracking | yes | yes | yes | map barriers plus sensors |
| checking, searching, stale, off_map | refused, running trip stopped | yes | yes | sensors only, dashboard says so |

Thresholds and tick counts are read off the recordings in phase 1, not
guessed. The bars are stored in `robot_nav.py` next to the pure functions,
with the recorded evidence noted in the comment.

### 3. Search: whole map, one scan, no spin

A RPLidar scan already covers 360 degrees. Spinning the robot adds no
information; it only gave AMCL's particles time to collapse. The search
instead scores one scan at every pose on every saved map:

- Precompute per map a soft lookup grid (distance-to-nearest-wall turned
  into a score, like a likelihood field, blurred to about half a metre so a
  coarse heading step cannot miss the true peak). Done once at map load and
  again whenever the learner rewrites the map.
- Coarse pass, one of two ways, decided by timing both on the robot in
  phase 2 and keeping the simpler one that is fast enough: (a) for each of
  72 headings at 5 degrees, rotate the scan's end points into a hit image
  and correlate it with the lookup grid by FFT, which gives the score at
  every translation for that heading at once; the grid must be padded to
  about 512 cells a side so a 12 m scan cannot wrap around the map's edge
  (72 correlations at 256 took 0.17 s on the robot in an ad-hoc test, so
  expect well under a second at 512); or (b) a plain grid of poses every 20
  cm and 10 degrees, each scored by the judge's own ray cast, no FFT, no
  padding, nothing new to get wrong.
- Keep the top twenty or so distinct peaks (merge peaks within a metre and
  20 degrees), refine each at 1 degree and one cell, then run the strict
  judge on each. The coarse score is the same "end points near walls" idea
  as today's defect, so the true pose may not be the coarse winner; the
  judge decides, by agree and through shares, never by raw coarse score, and
  the same shares rank candidates across maps.
- **The prior guides.** When the search starts from `tracking` the robot
  has a prior: the map-to-odom transform frozen at the last `tracking`
  tick, with live odometry added since. Not AMCL's live estimate, which
  wanders once it is lost. The prior's doubt grows with the distance driven
  since (a few percent of the distance, a few degrees per turn; the numbers
  come from the gamepad-drive recordings in phase 1). Candidates inside
  that doubt need little evidence to win; candidates outside it need strong
  evidence over motion. This is what keeps a hallway from turning into a
  relocalize.
- **Nothing detects a carry or a drag.** Arlen: "Do I know where I am? Is
  there another point that makes more sense? covers drag, carry,
  everything." If the robot was moved by hand, the candidates near the
  prior fail the judge, and the far candidates win instead. It is one
  search, not two: the whole map is always scored, the prior only decides
  how much evidence a candidate needs, so a carry costs nothing extra.
  Wheel readings during a drag are garbage anyway, whichever way it is
  pulled.
- **Believe, then confirm by motion.** If one candidate clearly beats the
  rest it is committed at once: seed AMCL, enter `checking`. `checking`
  becomes `tracking` only after the robot has moved and the judge still
  agrees; standing still and re-scoring the same scan proves nothing, and
  AMCL does not update until the robot has moved either. If two or more
  candidates remain close the state stays `searching` ("several
  candidates") and the search re-matches the survivors as the robot moves;
  each odometry-chained scan either strengthens or kills a candidate (the
  prism_loc pattern). The motion is whatever is already happening: a
  teleop drive or, when standing still, a short nudge of about 30 cm or 30
  degrees that the search asks the motion node for. Not two spins. Arlen
  approved the nudge: turn in place or forward, forward only when the
  forward distance sensor and both cliff sensors are clear, one nudge per
  search. The bridge must know the nudge is the localizer's own motion, not
  a reason to cancel a waiting trip (today any motion intent raises the nav
  lock and cancels). If the nudge did not separate the candidates, the state
  stays `searching` until a teleop drive or a second search request moves
  it; the dashboard says "drive me a little".
- **Which map.** The search runs on every saved map and the best confirmed
  candidate names the map. Switching maps is what it is today (restart
  map_server and AMCL on the new one) but decided by evidence, not by trying
  maps in turn.
- **Not on a map.** No candidate survives the judge on any map, the robot
  has moved about 2 m or tried for 60 s so the confirmation step has had its
  chance, and the judge still fails: `off_map`. Until then the state is
  `searching` ("nothing found yet") and the dashboard says it is waiting for
  motion. Once declared, the dashboard says so and offers mapping. The
  search reads every saved map from disk (the PGM files), not from `/map`,
  which only ever carries the loaded one.

Where it lives: a small `robot-localizer` unit of its own (approved),
because the search can take up to a second and the nav bridge's half-second
tick must not stall.
The bridge keeps everything it does today except the spin and the fit; it
reads the localizer's state and asks it for a search where it used to spin.
The state travels on a ROS topic, not the hub: the hub's 1 s stale rule
against a 2 Hz publisher is what made the rail flap during the floor tests,
and a localizer state that blinks stale would cancel trips for nothing. All the maths is pure numpy functions in `robot_nav.py` (or a
sibling module if it outgrows the file) with tests on synthetic and recorded
scans.

### 4. Learner: the map follows the house

Arlen: "If I move a chair or close a door or remove a coffee table, the map
gets updated and persisted. groundfloor is groundfloor. I don't want a
million different iterations of the same map."

**One map, changed in place.** `groundfloor.pgm` and its yaml are the map,
the same files mapping wrote. The learner changes their cells and writes
them back (to a temporary file, then renamed over the original, so a crash
mid-write cannot leave half a map) when the robot goes idle, and at most
once every few minutes while it is busy. No learned copy, no versions.
Places and barriers are unaffected: they live in map coordinates, and the
map's origin and size never move; only cells change.

**Four kinds of cell.** The PGM already has occupied (black), free (white)
and unknown (grey). The learner adds **sometimes**: a cell that has been seen
solid on some visits and open on others, which is what a door, a gate or a
chair that moves daily looks like. It is stored as a second grey the map
server reads as unknown, so Nav2 plans through it with caution and the live
scan paints it into the costmap when it is actually there. The judge treats
sometimes like unknown: no opinion, never a contradiction. The search's
lookup grid ignores it. Nothing else in the stack needs to know.

**What counts as evidence.** Only while `tracking` with a high agree share,
only cells within about 4 m of the lidar (ray accuracy falls off with
distance), only while moving slowly or standing. Each tick, every beam
casts a vote: cells it passed through vote free, the cell it ended in votes
occupied. Votes are grouped into **visits**: all the votes a cell receives
within one pass, where a pass ends when the robot has been away from the
area for 30 minutes. One lap of the kitchen is one visit, however many
scans it took. A cell changes state after it has been contradicted on three
separate visits (default; Arlen may change this) with no visit agreeing in
between. A cell that changes state and later changes back becomes sometimes,
and a sometimes cell returns to a fixed state only after ten consistent
visits. The counters live in one sidecar file beside the map
(`groundfloor.evidence.npz`), which is bookkeeping, not another map.

**What this does for the three examples.**

- Coffee table removed: on three visits the beams pass through its cells;
  they fade to free; from then on the judge sees no contradiction there. In
  the meantime the judge's tolerance covers them.
- Chair moved: its old cells fade to free as above; its new cells are hit
  on three visits and become occupied. A chair that moves daily never
  settles and becomes sometimes, which is honest.
- Door: solid one day, open the next. The judge's small-patch rule keeps
  the robot `tracking` while it looks at the open door, which is what lets
  the learner see the door at all. After one flip it is sometimes, and
  neither the judge nor the search is ever surprised by it again.

**Who reads the new map.** The bridge already asks map_server to load a map
file by its `load_map` service (it does this today for map switching and
for the keepout mask). After a write the learner asks for the same reload;
map_server republishes `/map`, and AMCL, the costmap and the search pick it
up. AMCL's behaviour on a map reload (it may re-initialise its particles
from its last pose) is checked in phase 4 and, if needed, the reload waits
for the robot to be idle and is followed by a seed from the current pose.

**Why it does not eat itself.** Learning from a slightly wrong pose smears
the map and a smeared map makes the next pose slightly wrong. The guards:
evidence only while `tracking` with high agreement, three separate visits
before any change, no change at all while the state is anything else, and
the sometimes state as the honest answer for cells that keep arguing. The
dashboard's Map card shows what changed in the last week as a coloured
overlay, so a wrong lesson is seen. If one is seen there is no undo copy by
Arlen's choice; a re-map of that room (mapping mode already exists) is the
correction.

### What this gives Arlen's four points

1. No spin: one scan, whole map, candidates in under a second, confirmed
   by ordinary motion or a small nudge.
2. Continuous: the judge runs every tick in every mode of driving; a
   contradiction starts a search without anyone pressing a button, from
   where odometry says the robot should be. A drag or a carry is just a
   contradiction whose nearby candidates fail. A featureless corner reached
   from a known spot is not a contradiction and disturbs nothing.
3. Which map: the search scores all maps and picks the one that survives.
4. Accurate: nothing is called `tracking` without a high agree share, a low
   spread-out through share, and motion the judge agreed through. A 55
   percent match of any kind is `searching` and says so.

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
There is no separate measuring phase: the recorder lands first in phase 1
and recordings accumulate during the floor tests we already do.

### Phase 1: the judge (two evenings)

- A `record` command in the bridge saves the latest scan, the lidar's map
  pose from TF, odometry and AMCL covariance to a JSON lines file under
  `~/.config/robot-pet/recordings/`. Truth comes from taped crosses on the
  floor: three or four spots whose map coordinates are measured once (set
  the robot on the cross, confirm the pose on a known-good day, write it on
  the tape). A recording at a cross carries that pose as its label;
  elsewhere Arlen's sentence is the label. Tonight's wrong room and the
  metre-off pose are the first two entries. Further recordings: true pose at
  the crosses and the places, right spot wrong heading, on the counter,
  carried, and a gamepad drive around the floor while correctly localized,
  recorded as a sequence so the prior's doubt numbers and the
  motion-confirmation step can be replayed, not just single snapshots.
- Time the lidar from rail on to first scan, once, and write the number
  here; today's 20 s is a waiting allowance in the code, not a measurement,
  and the trip's 30 s start allowance is sized around it.
- `tools/localization_replay.py` prints today's `scan_fit` and the new
  `agree` and `through` shares for every recording. That table goes into
  this document and the bars are read off it, with margin. Pass/fail for
  the judge itself: the worst true-pose spread-out through share must sit
  clearly below the best wrong-pose share across all recordings. If it does
  not, the fallback is an edge rule (ignore beams whose predicted hit is
  within two cells of a wall end or an unknown cell) before any bar is set;
  if that does not separate them either, this plan stops and is rethought
  rather than tuned.
- Map audit: the replay also draws where through-beams land at the
  known-true poses. Clusters on a few cells are map errors: fix those cells
  or re-map (doors in their usual state, nobody walking, slow drive). Spread
  everywhere means the tolerance is wrong, not the map. The audit's verdict
  on `groundfloor` goes into this document before any bar is set.
- `robot_nav.beam_check(...)`: the ray-cast score, pure numpy, tests on
  synthetic maps and on the recordings, which run in the normal suite.
- The state machine in the new localizer node; the bridge and dashboard
  read its state; `localized` becomes "state is tracking". Handed poses
  enter `checking`. A trip is only accepted in `tracking`; a drop out of it
  cancels the trip (the gate already exists, it just reads a better input).
- Dashboard: state word, agree and through shares, map name.
- Exit, on the floor: a metre off and the wrong room are never shown as
  `tracking`; a correct pose reaches `tracking` within three ticks of a
  seed and a short drive; a hand-dragged robot drops to `searching` within
  the chosen ticks;
  a correctly localized robot driven by gamepad through the hallway and
  into corners, past a moved chair and an open door, stays `tracking` the
  whole way. The relocalize spin still exists in this phase and now has a
  judge that rejects wrong rooms.

### Phase 2: the search (two to three evenings)

- Time the FFT pass (padded) against the plain grid pass on the robot;
  keep the simpler one that is under a second per map.
- `robot_nav.global_search(...)`: lookup grid, coarse pass, peak picking,
  refinement, judge filter. Tests on recordings:
  every labelled true pose must be the top confirmed candidate; the wrong
  room recording must not produce a confirmed candidate at the wrong room.
- Confirmation over odometry-chained scans with the motion gate; the nudge
  request to robot_motion when standing still, and the bridge taught that
  the nudge is not a reason to cancel a waiting trip.
- Relocalize becomes "search now": spin code deleted, `RELOCALIZE_SPINS`
  gone, Localize button stays as a manual trigger.
- All saved maps are searched; the winner names the map.
- Exit, on the floor: carried to twenty spots, set down, Localize pressed
  (phase 3 adds the trigger): the right candidate within 5 s where the spot
  is unambiguous and `tracking` within 20 s with at most one nudge, within
  10 cm and 5 degrees against the taped crosses, never a wrong room, zero
  misses; a search on the counter stays `searching`, never committed.

### Phase 3: always on (one to two evenings)

- Triggers for a search: spread-out through share high for a few ticks,
  boot with no remembered pose, a remembered or handed pose that fails
  `checking`. Nothing else; carries and drags arrive through the first.
- `stale` when the lidar goes off; the rail coming back enters `checking`.
  The lidar still comes on only when a trip is asked for (Arlen's rule), so
  the trip waits through spin-up and the check; the start allowance is
  sized from the measured spin-up.
- `off_map` after the search fails on every map and the robot has moved
  about 2 m or tried for 60 s.
- Nav2 coupling finished: `searching` mid-trip stops the trip; a re-find
  within 30 s with the goal still standing resumes the trip by itself;
  otherwise the trip is given up and the voice says so. Trips refused in
  every state but `tracking`; the barrier gate hands over to sensors only
  and the dashboard says so.
- The voice answers from the state: "I'm not sure where I am yet, give me a
  second" to a trip request while not tracking.
- Exit, on the floor: drag the robot mid-trip two metres and turn it: trip
  stops within two ticks, state goes `searching`, re-finds itself and
  resumes the trip, and the dashboard tells the story without a button
  press. Carry it to another room: same, minus the resume. Put it on the
  counter: `searching` then `off_map`, no trip accepted, voice says why. Lidar
  off, carry it, rail on: `stale` then `checking` then a search, never the
  old pose shown as tracking.

### Phase 4: the learner (two evenings)

- `robot_nav.map_votes(...)`: the per-tick vote from one scan at a tracking
  pose, pure numpy, reusing the judge's ray cast. `robot_nav.apply_visits(...)`:
  the visit grouping and the three-visit rule, with the sometimes state.
  Tests on synthetic maps: a removed box fades after three visits, not two;
  a flipping door becomes sometimes; nothing changes from a `checking` or
  `searching` pose. The lookup grid is rebuilt after every map write.
- The learner node (inside `robot-localizer`, it already has the scan, the
  pose and the state) keeps the counters, writes the PGM and the evidence
  file atomically when idle, and asks map_server to reload.
- AMCL's reaction to a map reload is measured on the robot and the reload
  is gated on idle plus a re-seed if it resets its particles.
- Dashboard Map card: last week's changes as an overlay.
- Exit, on the floor: remove the coffee table; after three visits on
  separate occasions the table is gone from the map and the judge's through
  share at that spot is back to zero. Open a door that was closed when
  mapped, then close it: after the flip the door cells read as sometimes.
  Localization accuracy is unchanged across the whole phase; the map's
  origin, places and barriers are untouched.

### Phase 5: later

- **Dock or boot anchor.** A known parking spot gives a free, strong prior
  at boot, the way vacuums use the dock.
- **Tracker swap.** Beluga AMCL if it gets packaged for our distro or AMCL
  misbehaves. Nothing above changes.

## Decisions

2026-10-26:

- The nudge is allowed: turn in place or forward, respecting the forward
  distance sensor and both cliff sensors, one per search.
- The localizer is its own unit, `robot-localizer`.
- No separate measuring evening; recordings are collected during normal
  floor tests, starting with the two wrong poses we already have.

2026-10-27 (the "Agreed with Arlen" list under Requirements, in short):

- Everyday changes never cost `tracking`; through-beams, not low agreement,
  are the only trigger; the search starts from the odometry prior.
- The map is audited against recordings before any bar is set.
- Targets: 10 cm, 5 degrees; commit in 5 s unambiguous, 20 s with a nudge;
  zero false tracking in twenty trials.
- A stopped trip resumes by itself after a re-find within 30 s, else the
  voice announces the give-up.
- Lidar off means `stale`; voice interaction delays lidar off by a minute
  but does not start it unless it triggers motion; emotes and in-place
  actions never wait for a fix (the lidar power rules get their own plan).
- Gamepad always allowed; trips only in `tracking`; barriers hand over to
  sensors while not tracking and the dashboard says so.
- `off_map` after about 2 m or 60 s; `searching` says it is waiting for
  motion until then.
- The voice reports the state; the dashboard shows candidates while
  searching.
- Map learning is part of the design (part 4, phase 4), not a later idea.
  One map file changed in place, no learned copies or versions, no undo
  copy; a re-map is the correction. Three separate visits before a cell
  changes, a visit ends after 30 minutes away, the dashboard shows a week of
  changes (defaults accepted).

2026-10-27, after the independent review:

- Door rule: through-beams in one small patch are "something changed
  here", not a contradiction. Arlen: yes, "as long as this is a fairly
  common pattern (think robovac) and not super janky".
- No carry or drag detection of any kind. Arlen: "Do I know where I am? Is
  there another point that makes more sense? covers drag, carry,
  everything." The `carried` state and the cliff/IMU lift idea are gone.
- Five states: `tracking`, `checking`, `searching` (with a reason),
  `stale`, `off_map`. Arlen: "reduce to the ones that actually convey useful
  information".
- A found pose is believed at once but is `checking` until motion confirms
  it; the two contradictory sentences in the earlier draft are resolved
  this way.
- The search prior is frozen at the last `tracking` tick and carried by
  odometry, never AMCL's live estimate.
- Ground truth is taped floor crosses with measured map coordinates.
- FFT versus plain grid search is settled by timing both on the robot.
- The lidar spin-up is timed, not assumed; it still comes on only when a
  trip asks for it.

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
