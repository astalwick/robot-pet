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
that we are missing: a **tracker** (AMCL, kept) plus a **judge** (a strict
score that counts beams passing *through* mapped walls, and a confirmation
step over a little motion before anything is believed) plus a **search**
(match one 360 degree scan against the whole map, every saved map, by
brute-force correlation, no spinning) plus, once those three hold on the
floor, a **learner** that keeps the one map true as the house changes. The
judge runs on every scan; a pose that fails it a few scans running is
dropped and the search runs until something passes. That one question, "do I
know where I am, and is there another spot that makes more sense", covers
drags, carries and reboots alike. No lift sensors, no carried state, no
detection of anything. From Arlen's chair the whole thing is invisible: the
lidar wakes with the wake word, a trip leaves without a pause or a wiggle,
and the robot speaks only when it has genuinely been moved somewhere it
cannot place. One thread in the nav bridge, one module of pure functions,
one scan count, five states, three phases plus the learner.

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
- Relocalize searches with AMCL's particle cloud: 5000 particles over the
  whole floor at every heading is far too thin, so it needs spins and still
  lands in the wrong room. (A global particle init with 50 000 particles is
  the textbook method and does work; the floor failure was the weak score
  plus too few particles. A direct search is still the better tool for us
  because it needs no motion and answers "which map".)
- AMCL corrects its pose only every 25 cm or 12 degrees of travel
  (`update_min_d`, `update_min_a`); between corrections it is odometry
  alone, which is too coarse for a 10 cm target. Its seed covariance (32 cm,
  15 degrees) is three times wider than the error of the pose handed in.

## How people actually do this

Every system that relocalizes reliably has the same three parts. A tracker
that follows the robot when its pose is roughly right. A global search that
finds candidates from a single scan without moving. A confirmation step that
refuses to believe a candidate until it has survived a little motion or
clearly beats every alternative.

**Trackers.** Nav2's AMCL is still the default localizer for a prior map
([Nav2 AMCL docs](https://docs.nav2.org/jazzy/configuration_and_development/configuration_guide/others/configuring_amcl/)).
Beluga AMCL is a modern drop-in reimplementation demoed on a Raspberry Pi 4B
([Beluga](https://github.com/Ekumen-OS/beluga)); slam_toolbox's localization
mode is more precise but needs a serialized pose graph and has no global
search ([README](https://github.com/SteveMacenski/slam_toolbox)); Cartographer's
pure localization carries a search but is in maintenance. Checked on the
robot 2026-10-27: the Lyrical apt index has no Beluga package and only
Cartographer's message package. Scan-to-map correlation every tick is the
other standard tracker; its known pitfall is sliding along a featureless
hallway unless a penalty pulls it toward the odometry pose.

**Global search from one scan.** The method is correlative scan matching:
score the scan at every candidate pose on a grid of positions and headings
and take the best. Olson made it real time with a two level search, choosing
the heading step so that the farthest beam end moves at most one coarse cell
between steps ([Olson 2009](https://april.eecs.umich.edu/pdfs/olson2009icra.pdf);
[Olson 2015, M3RSM](https://april.eecs.umich.edu/pdfs/olson2015scanmatch.pdf)).
Hess et al. made it fast enough for Cartographer's loop closure with
branch-and-bound over precomputed grids; the score is the sum of map
probabilities at the scan's end points, and candidates below a minimum score
(0.55 local, 0.6 global) are thrown out
([Hess 2016](https://research.google/pubs/pub45466)). Both build the coarse
grid by **max-pooling**, not blurring: the max over a window is an upper
bound on every fine pose inside the coarse cell, so the cell holding the true
pose scores at least as well as the true pose itself and cannot be lost to
pooling; wrong cells can still outscore it, which is why several peaks
survive to the fine level, not one. The closest reference design is
prism_loc (ROS 2 Humble, C++, 2026-07): branch-and-bound over the whole map,
top 8 modes, re-matched over odometry-chained scans with a motion gate of
0.3 m or 0.3 rad between counted scans, committed only at 90 percent of the
posterior; 97 of 100 synthetic kidnapped-robot queries succeeded at 38 ms
per search ([prism_loc](https://github.com/kjungmo/prism_loc),
[parameters](https://github.com/kjungmo/prism_loc/blob/main/PARAMS.md)).
Too new and Humble-only to depend on, right to copy the shape of.

**Knowing you are lost.** The classic fixes for the kidnapped robot problem
(Sensor Resetting Localization, Mixture MCL, Augmented MCL, which is what
AMCL's `recovery_alpha_*` is) all need a trustworthy "how well does the scan
fit here" signal, and the literature is blunt that overlap-style metrics
(ours) are fragile ([Reliable-loc survey](https://arxiv.org/pdf/2411.07815)).
The discriminating signal is the beam model's: compare the range the map
predicts with the range the lidar measured. A measured range longer than the
map allows means the beam went through a wall, which a right pose cannot do.
Its known weakness, and the reason likelihood fields were invented, is
brittleness in angle (Thrun, Burgard, Fox, Probabilistic Robotics ch. 6.3):
a 2 degree heading error at 5 m moves a beam 17 cm sideways, past a door
frame, and reads as "through". The cure is to compare against the
predictions of neighbouring beams too, in the direction that makes "through"
harder to claim, not easier.

**Robot vacuums.** Public technical detail is thin; Dreame's marketing says
its X50 "recognises which floor it is on using landmark detection"
([Dreame](https://www.dreametech.com/blogs/blog/multi-floor-mapping-robot-vacuum)).
Vacuums lean on three things: the dock is a known anchor pose, lift and
wheel-drop sensors tell them they were carried, and a 360 degree scan is
matched against each stored map on wake. We have no dock and choose not to
use a lift signal (see Decisions); the scan match is the part we build. The
one other standard piece, a gyro for heading between corrections, we already
have: the IMU's yaw feeds our odometry.

**Maps that learn.** slam_toolbox's lifelong mode is "highly experimental".
The research line is Dynamic Pose Graph SLAM
([Walcott-Bryant et al.](https://people.csail.mit.edu/kaess/pub/WalcottBryant12iros.pdf)).
For a one-floor house the simple version is a slow, confidence-gated
occupancy update of the map, which is what a vacuum effectively does when it
"adjusts" after you move a sofa.

## The goal

A robot that always knows where it is, without being told. It finds its
position on the map from where it stands, without spinning. It keeps
checking that position while it drives or is driven and fixes it when it is
wrong. It knows which saved map it is on. It never calls itself localized
when it is not. Carried, dragged mid-trip, rebooted: it works it out on its
own, and the dashboard tells the truth about what it believes. Later, the
map absorbs changes to the house, and the robot knows when it is on no map
at all.

One principle runs through the design: **failure triggers, prior guides,
motion confirms.** Only a pose that fails the judge starts a search; the
search is weighed from where odometry says the robot should be; and nothing
is believed until a little motion confirms it. A robot that knew where it
was and drove down a symmetric hallway is not lost: its scan still agrees
with the map, and the prior keeps it on the pose it drove to.

## Requirements

Arlen's, in his order:

1. Localizes without spinning in circles.
2. Keeps localizing while navigating or being driven, unless it is already
   confidently right.
3. Detects which map it is on.
4. Accurate above all: never says localized at a 55 percent match.

Later: learns changes to the map; knows when it is on no map at all.

Agreed with Arlen on 2026-10-27 (each is spelled out once, in The design):

- Everyday changes (a moved chair, an open door, a person, the dog) never
  cost `tracking`; the judge tolerates things closer than the map says.
- The map is audited before any bar is trusted; unknown is never a wall.
- Position within 10 cm and heading within 5 degrees while `tracking`; a
  clear candidate within 5 s of the first scan where the spot is
  unambiguous; zero false `tracking` over twenty carry trials, one miss is a
  bug. Truth comes from taped crosses, for testing only.
- A trip waits for the robot, never the other way round: held, not refused,
  and it leaves on its own; a trip stopped by a lost pose resumes on its
  own; one 30 s budget per trip bounds the waiting.
- Lidar off means belief stale. The lidar starts on the wake word, and only
  the lidar (Nav2 already follows the lidar; that is existing behaviour).
- Gamepad driving is never slowed or blocked; map barriers are a bonus over
  the cliff and forward sensors, never something the gamepad depends on
  ("lidar is additive to what we have").
- Not on a map needs evidence; an unmapped room is `searching` first.
- The voice speaks only when a human is needed, only canned lines, and
  nothing after an ordinary idle.
- The dashboard's face is a marker and one caption per state; everything
  else lives behind the existing debug toggle, including the Localize button
  once the search runs on its own.

Mine, added:

5. **Honest states, not a flag.** One of `tracking`, `checking`,
   `searching`, `stale`, `off_map`, plus which map; every state is earned by
   evidence, none set as a side effect of a button or a restart.
6. **Right beats fast, but invisible beats both.** A found pose seeds the
   tracker at once but is `checking` until the robot has moved and the judge
   still agrees; the motion is whatever is already happening (a trip leaving,
   a gamepad drive). The robot never moves on its own to localize.
7. **Safety follows the pose.** A failed pose stops a trip. Barriers are
   published from whatever pose TF has, except in `off_map`.
8. **Measured first.** Every threshold comes from recorded floor data,
   replayed offline, with the true answer labelled. Mechanisms whose need no
   recording has shown stay on the shelf.
9. **Fits the Pi 5.** Whole-map search under one second across all saved
   maps, the judge under ten milliseconds per scan, both in numpy (scipy is
   not on the robot).
10. **One count.** One number N of consecutive verdict scans decides
    everything the judge decides: when a pose is dropped, when it is earned,
    when a `checking` pose is believed enough to leave. The judge runs on the
    bridge's 0.5 s tick, so N scans is N/2 seconds.
11. **Honest surface.** One thread in the nav bridge and one module of pure
    functions carry the maths. The plan also touches, and says so: the
    bridge (recorder, judge, state, search thread, held trips, reasons on
    replies), the voice (speaks three canned lines, adds nothing), the
    dashboard (marker, caption, debug panel, Localize button moved into it,
    Save place gated), the replay tool, and AMCL's configuration (part 1).
    Nothing else in the stack changes.

## The design

Four parts; the first exists. The tracker follows, the judge doubts, the
search finds, the learner keeps the map true.

### 1. Tracker: AMCL, configured for our map

AMCL keeps publishing `map -> odom` so Nav2 is never without a frame. It is
re-seeded by us (an `initialpose`) whenever the search commits a pose, and
nobody reads its covariance as truth any more. Configuration changes, all
measured at the taped crosses in phase 1:

- `update_min_d` 0.25 to 0.1 and `update_min_a` 0.2 to 0.1: corrections
  every 10 cm or 6 degrees instead of 25 cm or 12; this is the number that
  actually bounds its accuracy on our map.
- `sigma_hit` 0.2 to 0.1: sharper discrimination on a 5 cm map, at the cost
  of faster depletion on map errors; `resample_interval` 2 if depletion
  shows.
- `max_beams` 60 to 120 (180 makes correlated beams overconfident).
- Seed covariance 0.1 m² and 0.07 rad² to 0.02 and 0.01: a 14 cm, 6 degree
  spread, a little wider than the search's own error. The seed is stamped
  with the scan's timestamp, not now, so AMCL applies the odometry since (a
  gamepad robot moves 36 cm in the second a search takes); the bridge already
  looks TF up at that stamp for the judge, so the "failed to transform
  initial pose in time" seen on the floor cannot recur.
- `recovery_alpha_slow` and `fast` to zero once the search exists (phase 3):
  two recovery mechanisms fighting is the classic failure. From then on AMCL
  cannot recover from a wrong seed by itself; the judge is the only way out,
  by design.

Whether AMCL then holds 10 cm and 5 degrees is measured, not assumed: phase
1's recordings at the crosses carry AMCL's pose beside the true one. If it
holds, the tracker question is closed. If not, the scan-to-map refine step
from part 3 becomes the tracker (on the shelf), decided at phase 1 exit.

### 2. Judge: a strict score and one count

The score replaces `scan_fit`. The RPLidar A1 gives about 1440 points a
turn; the judge uses every fourth, 360 beams at 1 degree spacing. For each,
from the lidar's map pose at the scan's timestamp (TF at that stamp, not the
latest: at 0.25 m/s half a second of lag is 12 cm), ray-cast across the map
in half-cell steps (so a one-cell diagonal wall cannot be slipped between)
to the first occupied cell. To blunt the beam model's brittleness in angle,
each beam is compared against the predictions of itself and its two
neighbours on either side (±2 degrees), in the direction that makes
"through" harder to claim:

- **agree**: measured within the tolerance of *any* of the five predictions
  (15 cm plus five percent of range to start; read off recordings).
- **through**: measured more than twice the tolerance beyond the *longest*
  of the five predictions, where that prediction is within 6 m. The beam
  went through a mapped wall at any heading the pose could plausibly have.
- **short**: anything else. Something unmapped is in the way. Tolerated;
  this is what a changing house looks like.
- **no opinion**: the ray reaches an unknown cell before any wall, or the
  lidar returned nothing (the A1 drops returns on black and matte surfaces,
  so a no-return where the map predicts a wall must never count as through).
  Beams that always hit the robot's own body are masked once, from a
  recording. A scan whose counted beams are fewer than about a third of the
  total has **no verdict** and counts for nothing anywhere: it neither
  passes nor fails a pose and advances no clock. A robot facing the open
  front door is uninformed, not lost.

Two numbers come out of a scan with a verdict: `agree` share and `through`
share, over counted beams. A pose **passes** a scan when agree ≥ A and
through ≤ T, and **fails** it otherwise. A right pose has high agree and low
through. A wrong room has middling agree and lots of through, which is
exactly the case today's score cannot see. A pose a metre off along a
hallway may have few through beams (only the end wall, a patch no wider than
an open door), so agree must carry that case; the metre-off recording is the
test of A, the wrong-room recording the test of T. From the counter, beams
either run over the mapped counter to the far walls (through) or hit the
cabinets at wrong distances (agree low); either way it fails. Ray casting
360 beams over at most 240 half-cell steps is one vectorised numpy step
array, under 10 ms on the Pi. Scans taken while turning faster than the
bridge's existing `SCAN_FIT_MAX_TURN_RADIANS` are skipped, as today (Nav2
turns at 0.8 rad/s, so every turning scan is skipped; skew at the allowed
rate is 1.5 degrees, inside the tolerance). The agree tolerance must sit at
least twice the tracker's measured error at the crosses, or an honest 10 cm
tracker reads as unsure.

Everyday life must pass: a person walking past is short beams, tolerated; an
open door is a through patch, and T must sit above the open-door recording's
share and below the wrong room's. Phase 1 measures whether plain shares
separate them; the patch rule on the shelf is the fallback, and if neither
separates them this plan stops and is rethought rather than tuned.

**The state machine** runs on every tick in localize mode and owns the only
`localized`-like word anyone else reads. One count N (about four, from
recordings) drives it:

- `tracking`: the pose has passed N consecutive verdict scans, and the robot
  has moved at least 0.3 m or 0.3 rad since the pose was handed over (a
  stationary robot can confirm nothing; prism_loc's gate).
- `checking`: a pose that has not earned `tracking`: just handed over (boot,
  a search result, the lidar coming back) or fresh from a failing scan.
  **Clean** when its latest verdict scan passed. A clean `checking` may start
  a trip; the trip's first half metre is the confirmation. A running trip
  continues through `checking`. The pose **fails** when it has failed N
  consecutive verdict scans; a pose that passes again before that is back
  to earning `tracking` (N passes), so a cluttered room cannot flap the
  marker every tick.
- `searching`: the pose failed, or there is no pose at all (after a reboot).
  A running trip has been stopped and held; new trips are held. The search
  runs once a second and commits a clear winner, which enters `checking`.
  The reason for the debug panel: "nothing passes", "several candidates",
  "nothing found yet".
- `stale`: the lidar is off. Entered on the lidar-off edge; the pose, if
  any, is kept and shown softly; the first scan after the wake word enters
  `checking` (pose) or `searching` (none).
- `off_map`: 30 s of judged scans while `searching` with no candidate
  passing on any map (judged: enough counted beams to score the survivors; a
  robot with no pose has no verdicts of its own, so the search's own scans
  are what count). The search keeps running; a clear winner leaves for
  `checking`.

Why this is enough. A contradiction (wrong room, drag, carry with the lidar
on) fails N scans in a row at once and goes straight to `searching`. A world
that stops matching (counter, unmapped room, a crowd) fails the same way,
only through agree instead of through. A symmetric hallway is neither: the
side and end walls agree, nothing goes through, and the robot stays
`tracking` on odometry. Floor run 1's lesson, never re-seed a tracking AMCL
mid-drive, is built in: the search commits only in `searching`, where the
pose has already failed. A carry with the lidar on is expected to fail
during the carry itself (beams over the furniture to walls behind); phase 1
records one in a furnished room and a bare one to check, and if either slips
past the judge the anchored scan check comes off the shelf.

What each state allows:

| state | trips | gamepad | emotes, in-place | barriers | save place |
| --- | --- | --- | --- | --- | --- |
| tracking | yes | yes | yes | from the pose, plus sensors | yes |
| checking | start when clean, else held; a running trip continues | yes | yes | from the pose, plus sensors | no ("drive me a little first") |
| stale | held until the first scan | yes | yes | from the last pose, plus sensors | no ("drive me a little first") |
| searching | held; a running trip is stopped and held | yes | yes | from TF's pose, plus sensors | no |
| off_map | refused, with the one sentence | yes | yes | sensors only | no |

Barriers come from whatever pose TF has in every state but `off_map`: a soft
or doubted pose is still the best guess, the barriers mark the steps (the
check 30 floor fix, kept), and the docstring's rule stands, a wrong stop
over no stop. One accepted wrinkle: a robot carried while asleep publishes
barriers from its old pose until the first search commits, so a robot that
was parked by the steps may stop once on its first gamepad push in the new
room; it clears itself within seconds.

Thresholds (A, T, the tolerance, N) are read off the recordings in phase 1,
not guessed, and stored in `robot_nav.py` next to the pure functions with
the recorded evidence noted in the comment, as `SCAN_FIT_*` is today.

### 3. Search: whole map, one scan, no spin

A RPLidar scan already covers 360 degrees. Spinning the robot adds no
information; it only gave AMCL's particles time to collapse. The search
instead scores one scan at every pose on every saved map:

- Precompute per map two lookup grids from distance-to-nearest-wall turned
  into a score (a likelihood field): a **coarse** one max-pooled over a 60 cm
  window, and a **sharp** one at 5 cm for refinement. Done once at map load
  and again whenever the learner rewrites the map.
- Coarse pass: a plain grid of poses every 20 cm over the map's free cells
  and every 3 degrees of heading, scored by summing coarse-grid values at
  the end points of every sixth beam (60 beams), capped at 6 m. The numbers
  fit together by Olson's rule: a 20 cm grid puts the true pose up to 14 cm
  from a grid pose, and a 3 degree step moves a 6 m beam end up to 16 cm, so
  a 30 cm half-window (60 cm) covers both and the true pose's cell cannot be
  lost to pooling. About 40 m² of free floor is 1000 poses, times 120
  headings, times 60 beams: 7 million gathers, expected 0.3 to 0.6 s on the
  Pi; measured in phase 2 against the one-second bar. If it misses, the
  knobs in order: cap the coarse pass at 4 m (then a 50 cm window), every
  twelfth beam, the FFT pass on the shelf. No ray casting here: ray casting
  every grid pose is billions of samples.
- Keep the top twenty distinct peaks (merge within one grid step, 20 cm and
  3 degrees; the merge radius must not exceed the refine window). Refine
  each on the sharp grid over ±20 cm and ±5 degrees at 5 cm and 1 degree on
  every third beam (120): about 900 poses times 120 beams times 20 peaks, 2
  million gathers. Then run the ray-cast judge on the twenty survivors, all
  360 beams, about 10 ms each. Twenty is the smallest count that keeps every
  labelled true pose in replay; if a true pose ever drops out of the coarse
  top twenty the coarse through term comes off the shelf. The coarse score
  is the same "end points near walls" idea as today's defect, so the true
  pose need not be the coarse winner; the judge decides, by agree and
  through shares, never by raw coarse score, and the same shares rank
  candidates across maps. Whole search, all maps: under one second.
- **Rules, written as formulas.** A candidate passes when agree ≥ A and
  through ≤ T (the judge's own bars). Among passing candidates the winner is
  the one with the highest agree, and it is **clear** when it is the only
  one passing or beats the next by at least M in agree share (start at 0.15,
  about the gap Cartographer and prism_loc use between accepted and rejected
  scores; measured). The **prior** is the map-to-odom transform frozen at
  the last `tracking` tick with live odometry added since; a candidate is
  **inside** it within one fixed radius (about 1 m and 30 degrees, read once
  off the gamepad-drive recording). An inside candidate wins by passing
  clearly among inside candidates. An outside candidate must pass and beat
  every inside passer by M; a carried robot's inside candidates all fail, so
  the far one wins at once; a robot a metre off has the true pose at the
  prior's edge, passing alone, and it wins on whichever side it falls.
- **No carried state, no lift sensor, no detector.** Arlen: "Do I know
  where I am? Is there another point that makes more sense? covers drag,
  carry, everything." It is one search, not two: the whole map is always
  scored, the prior only decides how much evidence a candidate needs, so a
  carry costs nothing extra. A seed from a mid-air scan is harmless: it
  enters `checking`, the wheels do not turn in the air so it cannot earn
  `tracking`, and the next search overwrites it when it fails.
- **Believe, then confirm by motion.** A clear winner is committed at once:
  seed AMCL, enter `checking`; `tracking` follows N passes and the 0.3 m /
  0.3 rad gate. If no winner is clear the state stays `searching` ("several
  candidates"), the trip stays held, and the search runs again a second
  later; as the robot moves, one fresh scan from a new spot breaks the tie.
  No candidate tracking across scans, no nudge: the robot never moves itself
  to localize. A tie nothing resolves runs out the held trip's 30 s and the
  give-up is spoken; the caption already says moving helps.
- **Which map.** The search runs on every saved map, read from the PGM
  files on disk (`/map` carries only the loaded one), and the winner names
  the map. Switching maps is what it is today (the bridge asks map_server to
  load the other file and re-seeds AMCL), decided by evidence instead of by
  trying maps in turn. A map load re-initialises AMCL at the origin, so the
  judge is muted from the load until the re-seed.
- **Not on a map.** No candidate passes on any map for 30 s of judged scans:
  `off_map`, as above.

Where it lives: one `threading.Thread` per search, started from the tick
when the state is `searching` or `off_map` and no search is running, writing
one result attribute that the next tick reads. Its own flag, not the
bridge's `busy` activity lock that relocalize uses today: an activity
refuses trips, pauses the state machine and resets the pending goal's clock
every tick, all of which the trip rules forbid. No queue, no result class,
no localizer object; the maths is pure numpy functions in `robot_nav.py`
(or a sibling module if it outgrows the file) with tests on synthetic and
recorded scans. The search takes up to a second and numpy releases the GIL
for its big gathers, but whether the tick and the barrier publisher stall is
measured in phase 2; if they do, the search moves to a subprocess.

**Held trips** reuse the pending slot the bridge already has
(`goal_request`, with `GOAL_START_SECONDS` = 30 s): the goal dict gains a
`state` of `held`, and a failed pose writes the running goal back into that
slot instead of cancelling it. One 30 s budget per trip covers all its
holds together; it starts at the first judged scan of the first hold (not at
the request, which would spend it on lidar spin-up) or at the stop, and the
bring-up clock is restarted when the hold releases so the two never
overlap. A released hold is a new Nav2 goal with a new serial (same target),
so a late cancel result from the stopped one cannot kill the resumed trip.
When the budget runs out the goal ends `failed` with reason `lost`. The
budget is what bounds the robot stuck on a rug: wheels turn, odometry
advances, the world does not, which reads as a failed pose, a re-find at the
true pose and a resume into the same rug; without the budget that loop is
silent and endless.

**The voice** needs no new channel. The bridge's reply to a trip request
carries `accepted: true` plus `held: <state>` when the trip waits, or the
refusal reason in `off_map`; the goal ends `failed` with `lost` on a
give-up, which the voice already learns from its existing poll, where `held`
counts as still in flight. The voice's trip tool speaks the canned line
itself and returns the same plain "started" result to the language model
that a normal trip returns, so the model has nothing to narrate about
position. The three lines, spoken verbatim and only in these cases:

- "Hang on, I'm working out where I am." Only when the hold's state is
  `searching`; a hold in `stale` or `checking` (the lidar still spinning up
  after an ordinary idle) says nothing. If this line is ever heard after an
  ordinary idle, that is the pre-trip pause leaking through the speaker, and
  the fix is the lidar rules, not a nicer sentence. A repeated request
  during the same hold hears it again, which is what a person would do.
- "I've lost track of where I am. Move me a little." When the trip tool's
  poll sees the goal go from in flight to `failed` with `lost`; never on a
  `failed` found at the start of a poll (the goal row never clears). A
  give-up after the voice has stopped polling is silent until the UX audit's
  cluster A gives the robot ears during trips; if the model adds a sentence
  of its own after this line, that is one extra sentence after a genuine
  failure, never after an idle.
- "I can't work out where I am from here. Put me on the floor somewhere I
  know." Only when a trip is asked for in `off_map`; never unprompted.

`where_am_i` returns the state word and the map, so the model reads rather
than guesses.

### 4. Learner: the map follows the house

Arlen: "If I move a chair or close a door or remove a coffee table, the map
gets updated and persisted. groundfloor is groundfloor. I don't want a
million different iterations of the same map."

Outline only; re-planned at phase 3 exit with the measured judge in hand.

- **One map, changed in place.** `groundfloor.pgm` and its yaml are the
  map; the learner rewrites cells (temporary file, then rename, when idle).
  Beside it: the evidence counters and one pristine copy made the first time
  the learner runs on a map, so a bad lesson is one click in the Map card,
  "forget what you learned", shown only when there is something to forget.
  The map's origin and size never move, so places and barriers are
  untouched.
- **Votes only from disagreeing beams**, only while `tracking`, within 4 m,
  hitting the wall at more than 20 degrees from grazing. A through beam votes
  free on the wall cell it crossed; a short beam votes occupied where it
  stopped; an agreeing beam votes nothing. This is what stops the learner
  eating itself: a pose 10 cm off for a whole lap casts no votes, because 10
  cm is inside the agree tolerance.
- **Visits and verdicts.** Votes on a cell until it has had none for 30
  minutes are one visit; a visit's verdict is clear at 20 votes, 90 percent
  one way; a cell changes after three visits against it with none for it in
  between (defaults accepted by Arlen).
- **Reload.** The bridge asks map_server to reload by the `load_map` service
  it already uses, judge muted, then re-seeds AMCL from the current pose and
  rebuilds the lookup grids.

### What this gives Arlen's four points

1. No spin: one scan, whole map, candidates in under a second, confirmed by
   the first half metre of the trip or whatever drive is already happening.
2. Continuous: the judge runs on every scan in every mode of driving; a pose
   that fails N scans starts a search without a button, weighed from where
   odometry says the robot should be. A symmetric hallway driven from a
   known spot disturbs nothing.
3. Which map: the search scores all maps and picks the one that survives.
4. Accurate: nothing is `tracking` without a high agree share, a low through
   share, and motion the judge agreed through; AMCL is configured so it can
   actually hold 10 cm. A 55 percent match of any kind is `searching`, and
   the marker says so.

## Alternatives considered

Rejected:

- **Move the bars, or weight AMCL's covariance in.** The wrong room scored
  inside the true range; no bar fixes a score that does not measure the
  right thing.
- **slam_toolbox localization mode.** No global search, a re-map into pose
  graphs, heavy tuning, and it would still need parts 2 and 3.
- **Cartographer pure localization.** Has the search, but not packaged for
  Lyrical, Lua configuration, maintenance mode.
- **Adopt prism_loc.** Right shape, but Humble, C++, three months old. We
  copy its verification pattern and keep our stack.
- **Keep spinning but spin smarter.** Spinning is a workaround for a search
  tool that is not a search tool. Removing it is the point.
- **Two judge cases** (contradiction versus nothing matches) with different
  counts and a near-re-seed ban. One count and "commit only in `searching`"
  do the same job with one rule; the near-re-seed ban falls out of it.
- **Three states instead of five.** `stale` and `off_map` are two
  transitions and two captions each; they tell the person at the marker
  different things.
- **A separate `robot-localizer` systemd unit.** The search's one need, not
  stalling the tick, is a thread (or at worst a subprocess) inside the
  bridge.
- **Tracking candidates across odometry-chained scans** (the full prism_loc
  posterior). A multi-hypothesis tracker's worth of code to break ties that
  one fresh search from a new spot breaks anyway in a house.
- **A one-turn nudge** for tied candidates. Cut by Arlen; the robot never
  moves itself to localize.
- **A gamepad speed cap while unsure.** Struck by Arlen: lidar is additive.
- **A resume count** on held trips. One time budget bounds the same loop
  with one number fewer.

On the shelf, one line each, built only when the named recording shows the
simpler thing failing:

- **Patch rule**: group through beams into angular patches, drop the largest
  one or two, count the rest; if the open-door recording's through share
  nears the wrong room's.
- **Edge rule**: ignore beams predicted to hit within two cells of a wall end
  or an unknown cell; if true-pose through shares stay noisy after the
  neighbour rule.
- **Coarse through term**: two samples along each beam in a dilated wall
  grid, subtracted; if a labelled true pose misses the coarse top twenty.
- **FFT correlation pass** for the coarse search (about 0.75 s at 512 cells,
  extrapolated from a 0.17 s test at 256); if the plain grid misses the
  one-second bar after its own knobs.
- **AMCL global init with 50 000 particles**; if the numpy search cannot be
  made to fit the Pi.
- **Anchored scan check** (first scan of a still period kept and compared
  against each new one); if a phase 1 carry recording slips past the judge.
- **Refine-step tracker** (the sharp-grid refine run every scan around the
  odometry pose, with a penalty toward it, replacing AMCL); if AMCL misses
  10 cm or 5 degrees at the crosses in phase 1.
- **"Sometimes" cells** (a second grey the map server reads as unknown,
  pixel 100 to 180 so both `free_thresh` 0.196 and 0.25 leave it unknown);
  if the learner's three-visit rule is seen flapping on a door.

## Phases

Each phase ends with something Arlen can see on the floor or in a replay.
There is no separate measuring phase: the recorder lands first in phase 1
and recordings accumulate during the floor tests we already do. The evening
counts carry the lesson of the first floor evening (far smaller changes
produced three fix batches).

### Phase 1: the judge (four to five evenings)

- A `record` command in the bridge saves the latest scan, the lidar's map
  pose from TF at the scan's stamp and odometry to a JSON lines file under
  `~/.config/robot-pet/recordings/`. Truth comes from three or four taped
  crosses, each tape-measured from two mapped walls with a taped heading
  line; a recording at a cross carries that pose as its label, elsewhere
  Arlen's sentence is the label. Tonight's wrong room and the metre-off
  pose are the first two entries. Further recordings: true pose at the
  crosses and the places, right spot wrong heading, on the counter, on the
  rug (tilt lets beams see over low furniture), a carry with the lidar on
  through a furnished room and a bare one, a drag along the hallway, a
  gamepad drive around the floor while correctly localized (as a sequence,
  for the prior radius and the motion gate), and a drive past the open door
  and the dark sofa.
- `tools/localization_replay.py` prints today's `scan_fit` and the new
  `agree` and `through` shares for every recording, AMCL's error at each
  cross, and which angular sectors are always short (the robot's own body,
  masked once). That table goes into this document and A, T, the tolerance
  and N are read off it, with margin. Pass for the judge: every true pose
  (the open-door and rug recordings count as true) passes and every wrong
  pose fails, with daylight between. If not, the patch rule comes off the
  shelf, then the edge rule; if neither separates them, this plan stops and
  is rethought rather than tuned. The carry recordings decide the anchored
  check; the cross recordings decide the tracker.
- Map audit: the replay draws where through beams land at the known-true
  poses. Clusters on a few cells are map errors: fix those cells or re-map
  (doors in their usual state, nobody walking, slow drive); if unknown
  speckle inside rooms swallows beams, fill unknown islands that do not touch
  the border at map load. Spread everywhere means the tolerance is wrong, not
  the map. The verdict on `groundfloor` goes into this document before any
  bar is set.
- `robot_nav.beam_check(...)`: the ray cast at half-cell steps, the
  five-beam neighbour rule, no-return and unknown as no opinion, the
  no-verdict rule; pure numpy, tests on synthetic maps and on the
  recordings, which run in the normal suite.
- The state machine in the bridge with three states, `tracking`, `checking`,
  `searching`, one count N, the motion gate, handed poses entering
  `checking`. `localized` becomes "tracking or clean checking". A failed
  pose cancels a running trip through the gate that already exists. Save
  place needs `tracking`, with its refusal text beside the button. A trip
  asked for while `searching` waits in the existing pending slot for its 30
  s and then fails `not_localized`, honestly; the automatic spin before a
  trip is removed now, because the old spin judged strictly would spin twice
  and fail more often than today. The Localize button still runs the old
  spin, with the judge deciding whether the result is believed, so a wrong
  room is never accepted; "worse" in this phase means a wrong room accepted,
  not fewer trips started.
- AMCL: `update_min_d` and `update_min_a` 0.1, `sigma_hit` 0.1, `max_beams`
  120, seed covariance 0.02 and 0.01, seed stamped with the scan's time.
- Dashboard: the marker (solid, soft, hollow) and the caption in front;
  state word, shares, map name behind the debug toggle.
- Exit, on the floor: a metre off and the wrong room are never shown as
  `tracking`; a correct pose reaches `tracking` within N scans after a seed
  and a short drive; the counter drops to `searching` within N scans; a
  hand-dragged robot drops to `searching`; a robot parked nose to a wall or
  under the desk still leaves on a trip at once; a correctly localized robot
  driven by gamepad through the hallway and into corners, past a moved
  chair, an open door, over the rug and past the dark sofa, stays `tracking`
  the whole way; AMCL's error at the crosses is written down.

### Phase 2: the search (three to four evenings)

- `robot_nav.global_search(...)`: the two lookup grids, the coarse pass at
  20 cm and 3 degrees on 60 beams, merge at one grid step, top twenty,
  sharp refinement on 120 beams, judge on the survivors, the pass/clear/prior
  formulas. Timed on the robot against the one-second bar for all maps
  together, and the tick and barrier publisher watched for stalls while it
  runs. Tests on recordings: every labelled true pose is in the coarse top
  twenty and is the clear winner after the judge; the wrong room recording
  produces no passing candidate at the wrong room; the counter recording
  produces none.
- Relocalize becomes "search now" on the thread, not an activity; spin code
  deleted, `RELOCALIZE_SPINS` gone. The Localize button stays in front only
  until phase 3 gives the search its own trigger.
- All saved maps are searched; the winner names the map; the judge is muted
  across a map load.
- Exit, on the floor: carried to twenty spots, set down, Localize pressed:
  the right candidate within 5 s where the spot is unambiguous, `tracking`
  within the first half metre of a trip, never a wrong room, zero misses (one
  miss is a bug to find in the recordings, not a statistic); at the crosses,
  within 10 cm and 5 degrees; a search on the counter stays `searching`,
  never committed; a tie (the two matching corners of the living room, if
  there are any) breaks within a metre of gamepad driving.

### Phase 3: always on (three evenings, one exit blocked)

- `searching` runs the search once a second on its own; boot with no
  remembered pose searches too.
- The pending goal grows its `held` state and the one 30 s budget; a failed
  pose moves the running goal into it instead of cancelling; a release is a
  new serial; the trip leaves on its own from `tracking` or a clean
  `checking`. The reply carries `held` or the refusal reason; the give-up
  ends the goal `failed` with `lost`. The voice's trip tool speaks the three
  canned lines from those fields and returns a plain result; `held` counts as
  in flight in its poll; `where_am_i` returns the state word.
- `stale` on the lidar-off edge and `off_map` after 30 s of judged scans
  with nothing passing. The Localize button moves behind the debug toggle.
- AMCL `recovery_alpha_slow` and `recovery_alpha_fast` to zero; the search
  owns recovery now.
- Dashboard: the goal row shows "held" and "resuming: kitchen"; a refused
  trip from a map click shows in the goal row, not the log; the `off_map`
  caption points at the existing Start mapping button, no new button.
- The lidar starts on the wake word. This needs the lidar power plan, which
  does not exist yet (today the lidar follows the rail, which powers the
  motors); until it exists the first trip after an idle still waits for the
  lidar and says nothing, because that hold is `stale`, not `searching`.
  Written down so it is not read as a bug later: once it exists, every wake
  word spins the lidar audibly and brings Nav2 up for about a minute.
- Exit, on the floor: say "go to the kitchen" from idle: the robot leaves
  with no wiggle and says nothing (no pause at all needs the lidar power
  plan). Drag the robot mid-trip two metres and turn it: the trip stops
  within N scans, the state goes `searching`, it re-finds itself and
  resumes, and the marker and caption tell the story without a button press.
  Carry it to another room: same, minus the resume. Put it on the counter
  and ask for a trip: `searching`, then `off_map` after 30 s, the one
  sentence, no trip. Lidar off, carry it, wake word: `stale`, `checking`, a
  search, never the old pose shown as tracking. Park it on the rug so it
  slips: stops and resumes end with the give-up line inside the 30 s
  budget, not silence. A drive past the dark sofa and through the hallway
  at speed never stops a trip. Nothing about position is ever said twice.

### Phase 4: the learner (re-planned at phase 3 exit; about three evenings)

- `robot_nav.map_votes(...)` (votes from one scan's disagreeing beams at a
  tracking pose, reusing the judge's ray cast) and
  `robot_nav.apply_visits(...)` (visits, verdicts, the three-visit rule).
  Tests on synthetic maps: a removed box fades after three visits, not two;
  a wall under a constant 10 cm pose offset never moves; nothing changes from
  a `checking` or `searching` pose.
- The learner runs in the bridge, writes the PGM and the evidence file
  atomically when idle, reloads with the judge muted, re-seeds AMCL, rebuilds
  the lookup grids. The pristine copy is made on first run if missing.
- Map card: "forget what you learned", with a count, shown only when there
  is something to forget; the change overlay is in the debug panel only.
- Exit, on the floor: remove the coffee table; after three visits on
  separate occasions it is gone from the map and the judge's through share
  there is back to zero. Localization accuracy is unchanged across the
  phase; the map's origin, places and barriers are untouched.

Total: ten to twelve evenings for localization that just works, plus about
three for the learner.

## Dashboard words

One marker and one caption per state, in front of the debug toggle:

| state | marker | caption |
| --- | --- | --- |
| tracking | solid | none |
| checking | soft | "checking my position" |
| searching | hollow, at TF's pose | "finding my place, moving helps" |
| stale | soft, at the last pose, or none | "lidar off" |
| off_map | none | "can't find my place here. New room? Start mapping below." |

Save place refusal beside the button: "drive me a little first". Goal row:
"held", "resuming: kitchen". Behind the toggle: state word, agree and
through shares, the reason text, the candidate list, the Localize button,
and the learner's change overlay.

## Decisions

The settled list. The dated history of how each was reached is in git
(`git log -- "docs/plans/2026-10-26 - localization-plan.md"`).

Arlen's, by his word:

- No carry or drag detection of any kind; "do I know where I am, is there
  another point that makes more sense" covers everything.
- Everyday changes never cost `tracking`; a door's through beams in one
  patch are "something changed", not a contradiction, as long as the rule is
  a common pattern and not janky.
- Five states.
- The one-turn nudge is cut; the robot never moves itself to localize.
- Auto-resume of a stopped trip stays.
- The lidar starts on the wake word; only the lidar.
- The gamepad is never slowed or blocked.
- The Localize button lives behind the debug toggle once the search runs on
  its own.
- The lidar stays on a minute after a conversation.
- Map learning is part of the design: one map changed in place, no learned
  copies or versions; a hidden mapping-day original and "forget what you
  learned" as the correction; three visits, 30 minute visit gap.
- No separate measuring evening; recordings come from normal floor tests.
- Dashboard words in front are Claude's call.

Claude's, as final arbiter after five review rounds:

- AMCL stays the tracker, with `update_min_d`/`a` 0.1 as the accuracy fix
  and `sigma_hit` 0.1 as a trial; the refine-step tracker is on the shelf,
  decided by AMCL's measured error at the crosses.
- One judge rule and one count N: a pose that fails N consecutive verdict
  scans is dropped; the search commits only in `searching`. Through is
  claimed only against the longest neighbouring prediction; agree against
  any.
- No-verdict scans count for nothing anywhere.
- The search is a thread in the bridge with its own flag, one result
  attribute, not an activity and not a unit; held trips reuse `goal_request`
  with one 30 s budget per trip and no resume count; a release is a new
  serial.
- Coarse pass at 20 cm, 3 degrees, 60 beams, 60 cm pool window (Olson's
  rule); top twenty, merged at one grid step, refined on 120 beams over ±20
  cm and ±5 degrees; one margin M in agree share; ties broken by re-running
  the search.
- Three canned lines ride the existing trip reply and poll; the voice tool
  speaks them, returns a plain result, and speaks the hold line only for a
  `searching` hold.
- Barriers from TF's pose in every state but `off_map`.
- Marker plus one caption per state in front; everything else behind debug.
- Phase 1 ships three states and removes the automatic pre-trip spin;
  `stale` and `off_map` arrive with their triggers in phase 3.
- Learner votes only from disagreeing, non-grazing beams within 4 m;
  "sometimes" cells on the shelf; built last, re-planned at phase 3 exit.

## Sources

- [Olson 2009, Real-time correlative scan matching](https://april.eecs.umich.edu/pdfs/olson2009icra.pdf), [Olson 2015, M3RSM](https://april.eecs.umich.edu/pdfs/olson2015scanmatch.pdf)
- [Hess et al. 2016, Real-Time Loop Closure in 2D LIDAR SLAM](https://research.google/pubs/pub45466)
- [prism_loc, 2D MCL with branch-and-bound global relocalization and verification](https://github.com/kjungmo/prism_loc), [parameters](https://github.com/kjungmo/prism_loc/blob/main/PARAMS.md)
- [Nav2 AMCL configuration](https://docs.nav2.org/jazzy/configuration_and_development/configuration_guide/others/configuring_amcl/), [ReinitializeGlobalLocalization BT node](https://docs.nav2.org/configuration/packages/bt-plugins/actions/ReinitializeGlobalLocalization.html)
- [Beluga AMCL](https://github.com/Ekumen-OS/beluga), [ROSCon 2024 slides](https://roscon.ros.org/2024/talks/Beluga_AMCL-_A_modern_Monte_Carlo_Localization_implementation_for_ROS.pdf)
- [slam_toolbox README](https://github.com/SteveMacenski/slam_toolbox), [JOSS paper](https://joss.theoj.org/papers/10.21105/joss.02783.pdf)
- [Cartographer tuning](https://google-cartographer-ros.readthedocs.io/en/latest/tuning.html)
- [Thrun et al., Robust Monte Carlo localization (Mixture MCL)](https://www2.cs.sfu.ca/~vaughan/teaching/415/papers/thrun_robust.pdf); Thrun, Burgard, Fox, Probabilistic Robotics, ch. 6.3 (beam model) and ch. 8.3.5 (Augmented MCL)
- [Sensor Resetting Localization summary](https://www.scitepress.org/Papers/2006/12158/12158.pdf)
- [Reliable-loc, survey of localization failure detectors](https://arxiv.org/pdf/2411.07815)
- [Dreame multi-floor guide](https://www.dreametech.com/blogs/blog/multi-floor-mapping-robot-vacuum), [Valetudo multi-floor thread](https://community.home-assistant.io/t/multi-floor-vacuuming-using-valetudo-and-dreame-robots/933706)
- [Walcott-Bryant et al., Dynamic Pose Graph SLAM](https://people.csail.mit.edu/kaess/pub/WalcottBryant12iros.pdf), [Zhao et al. 2021, lifelong localization and mapping framework](https://arxiv.org/pdf/2111.10946)
