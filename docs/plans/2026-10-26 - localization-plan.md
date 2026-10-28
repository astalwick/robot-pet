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
score that counts beams passing *through* mapped walls, a check for a
better spot nearby, and a confirmation step over a little motion before
anything is believed) plus a **search** (match one 360 degree scan against
the whole map, every saved map, by brute-force correlation, no spinning)
plus, once those three hold on the floor, a **learner** that keeps the one
map true as the house changes. The judge runs on every tick; a pose that
fails it a few scans running is dropped and the search runs until something
passes. Arlen's two questions, "do I know where I am, and is there another
spot that makes more sense", are asked literally, every tick, and cover
drags, carries, hallway slips and reboots alike. No lift sensors, no
carried state, no detection of anything. From Arlen's chair the whole thing
is invisible: the lidar wakes with the wake word, a trip leaves without a
pause or a wiggle, and the robot speaks only when it has genuinely been
moved somewhere it cannot place. One thread in the nav bridge, one module of
pure functions, one scan count, one margin, five states, three phases plus
the learner.

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
  (`update_min_d`, `update_min_a`); between corrections the published pose
  is stale odometry. Its seed covariance (32 cm, 15 degrees) is three times
  wider than the error of the pose handed in. And the bridge reads AMCL's
  belief a second late: AMCL post-dates its `map -> odom` transform by
  `transform_tolerance` (1.0 s), so a TF lookup at the scan's own time
  returns the belief from the scan one second earlier.

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
and take the best. Olson made it real time with a two level search over a
max-pooled coarse grid ([Olson 2009](https://april.eecs.umich.edu/pdfs/olson2009icra.pdf);
[Olson 2015, M3RSM](https://april.eecs.umich.edu/pdfs/olson2015scanmatch.pdf)).
Hess et al. made it fast enough for Cartographer's loop closure with
branch-and-bound over precomputed grids, choosing the heading step so that
the farthest beam end moves at most one map cell between steps; the score is
the sum of map probabilities at the scan's end points, and candidates below
a minimum score are thrown out (the rule is Hess's; Cartographer's shipped
numbers are 0.55 local and 0.6 global, in its `pose_graph.lua`)
([Hess 2016](https://research.google/pubs/pub45466)). Both build the coarse
grid by **max-pooling**, not blurring: the max over a window is an upper
bound on every fine pose inside the coarse cell, so the cell holding the true
pose scores at least as well as the true pose itself and cannot be lost to
pooling; wrong cells can still outscore it, which is why several peaks
survive to the fine level, not one. The closest reference design is
prism_loc (ROS 2 Humble, C++, 2026-07): branch-and-bound over the whole map,
top 8 modes, re-matched over odometry-chained scans with a motion gate of
0.3 m or 0.3 rad between counted scans (the README's recommendation; the
shipped default is zero), committed only at 90 percent of the posterior; 97
of 100 synthetic kidnapped-robot queries succeeded at 38 ms per search
([prism_loc](https://github.com/kjungmo/prism_loc),
[parameters](https://github.com/kjungmo/prism_loc/blob/main/PARAMS.md)).
Too new and Humble-only to depend on, right to copy the shape of.

**Knowing you are lost.** The classic fixes for the kidnapped robot problem
(Sensor Resetting Localization, Mixture MCL, Augmented MCL, which is what
AMCL's `recovery_alpha_*` is) all need a trustworthy "how well does the scan
fit here" signal. The discriminating signal is the beam model's: compare the
range the map predicts with the range the lidar measured. A measured range
longer than the map allows means the beam went through a wall, which a right
pose cannot do. Its known weakness, and the reason likelihood fields were
invented, is brittleness in angle (Thrun, Burgard, Fox, Probabilistic
Robotics ch. 6.3): a 2 degree heading error at 5 m moves a beam 17 cm
sideways, past a door frame, and reads as "through". The cure is to compare
against the predictions of neighbouring beams too, in the direction that
makes "through" harder to claim, not easier. And one class of error no
single-scan score can see: a shift along a plain corridor, where the side
walls agree at every position and only the far end wall disagrees by a few
beams. Every scan matcher aliases there; the honest answer is to ask
directly whether a nearby pose fits better.

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
The standard practice in every lifelong grid mapper is the same two gates:
update the map only from poses the localizer itself accepts, and update each
cell slowly, counting confirming hits as well as misses, so one bad pass
cannot flip it. That is what a vacuum effectively does when it "adjusts"
after you move a sofa.

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
with the map, no nearby pose fits better, and the prior keeps it on the pose
it drove to.

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
  own; one 30 s deadline per trip bounds the waiting.
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
7. **Safety follows the pose.** A dropped pose stops a trip. Barriers are
   published from whatever pose TF has, except in `off_map`.
8. **Measured first.** Every bar comes from recorded floor data, replayed
   offline, with the true answer labelled. A few geometric constants are
   design choices and are named as such. Mechanisms whose need no recording
   has shown stay on the shelf.
9. **Fits the Pi 5.** Whole-map search under one second per map, the judge
   under fifty milliseconds per tick, both in numpy (scipy is not on the
   robot).
10. **One count, one margin.** One number N of consecutive verdict scans
    decides when a pose is dropped and when it is earned; one margin M
    decides when one pose clearly beats another. The judge runs on the
    bridge's 0.5 s tick, so N scans is N/2 seconds.
11. **Honest surface.** One thread in the nav bridge and one module of pure
    functions carry the maths. The plan also touches, and says so: the
    bridge (recorder, judge, state, search thread, held trips, reasons on
    replies), the voice (the trip tool waits out a hold and speaks three
    canned lines through the runner's existing progress speaker), the
    dashboard (marker, caption, debug panel, Localize button moved into it,
    Save place gated), the replay tool, and AMCL's configuration (part 1).
    One outside dependency: the lidar power plan (not yet written) for the
    wake-word lidar. Nothing else in the stack changes.

## The design

Four parts; the first exists. The tracker follows, the judge doubts, the
search finds, the learner keeps the map true.

### 1. Tracker: AMCL, configured for our map

AMCL keeps publishing `map -> odom` so Nav2 is never without a frame. It is
re-seeded by us (an `initialpose`) whenever the search commits a pose, and
nobody reads its covariance as truth any more. Configuration changes, all
measured at the taped crosses in phase 1:

- `update_min_d` 0.25 to 0.1 and `update_min_a` 0.2 to 0.1: corrections
  every 10 cm or 6 degrees instead of 25 cm or 12, so the published pose is
  never more than that stale. The heading requirement is 5 degrees; if the
  crosses show 6 degrees of staleness is what breaks it, `update_min_a`
  drops to 0.08.
- `sigma_hit` 0.2 to 0.1: a sharper per-beam weight on a 5 cm map (Nav2 sums
  beam weights, so the beam count barely matters and `max_beams` stays), at
  the cost of faster depletion on map errors; `resample_interval` 2 if
  depletion shows.
- Seed covariance 0.1 m² and 0.07 rad² to 0.02 and 0.01: a 14 cm, 6 degree
  spread, a little wider than the search's own error.
- **Seed transport.** A search takes up to a second, in which a gamepad
  robot moves 36 cm, or a metre at turbo. AMCL's own seed handling cannot
  be relied on to carry the pose forward: it looks the odometry up against
  its own clock, with no timeout, and when the lookup fails (the "failed to
  transform initial pose in time" seen on the floor) it silently plants the
  seed as the pose *now*. So the bridge does the carrying: the found pose is
  moved forward by the odometry between the scan's stamp and the newest
  `odom -> base_link` the bridge holds, and the seed is stamped with that
  newest odometry time. Whatever AMCL's lookup does, the error is one
  odometry period, about 2 cm.
- `recovery_alpha_slow` 0.001 and `fast` 0.1 to zero in the same phase that
  ships the search (phase 2): two recovery mechanisms fighting is the
  classic failure. From then on AMCL cannot recover from a wrong seed by
  itself; the judge is the only way out, by design.

Whether AMCL then holds 10 cm and 5 degrees is measured, not assumed: phase
1's recordings at the crosses carry AMCL's pose beside the true one. If it
holds, the tracker question is closed. If not, the scan-to-map refine step
from part 3 becomes the tracker (on the shelf), decided at phase 1 exit.

### 2. Judge: a strict score, a rival check, one count

**The pose it judges.** The lidar's map pose at the middle of the scan,
composed from the *latest* `map -> odom` (AMCL's current belief; a lookup
at the scan's own time would return the belief from a second earlier, as
noted above) and `odom -> laser` at the scan's stamp plus half its duration
(at 0.25 m/s half a second of lag is 12 cm). After a seed, the first scan
stamped after the seed is the first that counts; AMCL publishes its new
belief on that scan.

**The score** replaces `scan_fit`. The RPLidar A1 gives about 1440 points a
turn; the judge uses every fourth, 360 beams at 1 degree spacing. For each,
ray-cast across the map in half-cell steps to the first occupied cell (a
one-cell wall at an angle can still show corner gaps; the map audit looks
for through clusters along diagonal walls). To blunt the beam model's
brittleness in angle, each beam is compared against the predictions of
itself and its two neighbours on either side (±2 degrees), in the direction
that makes "through" harder to claim:

- **agree**: measured within the tolerance of *any* of the five predictions
  (20 cm plus five percent of range to start; read off recordings, and at
  least twice the tracker's measured error at the crosses, or an honest 10
  cm tracker reads as unsure).
- **through**: measured more than twice the tolerance beyond the *longest*
  of the five predictions, where that prediction is within 6 m. The beam
  went through a mapped wall at any heading the pose could plausibly have.
- **short**: anything else. Something unmapped is in the way. Tolerated,
  but counted: this is what a changing house looks like, and also what a
  crowd or an unmapped room looks like, which is why agree can fail.
- **no opinion**: the ray reaches an unknown cell before any wall, or the
  lidar returned nothing (the A1 drops returns on black and matte surfaces,
  so a no-return where the map predicts a wall must never count as through).
  Beams that always hit the robot's own body are masked once, from a
  recording. A scan whose counted beams are fewer than a third of the total
  (a design constant) has **no verdict** and counts for nothing anywhere: it
  neither passes nor fails a pose and advances no clock. A robot facing the
  open front door is uninformed, not lost.

Two numbers come out of a scan with a verdict: `agree` share and `through`
share, over counted beams. A pose **passes** a scan when agree ≥ A and
through ≤ T. A right pose has high agree and low through. A wrong room
fails on through, and so does a right spot at the wrong heading: both put
walls at plausible distances (their agree may well sit above A, which is
exactly what today's score cannot see) but send beams through mapped walls.
From the counter, beams either run over the mapped counter top to the far
walls (through) or hit the cabinets at wrong distances (agree low); either
way it fails. Scans taken while turning faster than the bridge's existing
`SCAN_FIT_MAX_TURN_RADIANS` are skipped, as today (Nav2 turns at 0.8 rad/s,
so every turning scan is skipped; at the allowed rate the A1's 133 ms
revolution skews the scan ±1.5 degrees about its middle, inside the ±2
degree neighbour rule).

**The rival check** asks Arlen's second question on every verdict scan: is
there a nearby pose that fits better? Around the current pose, within 1.5 m
and 30 degrees (wider than any slip the floor has shown; the hallway
recording confirms), score the neighbourhood at 20 cm and 3 degree spacing
on the search's pooled coarse grid (about 180 positions by 21 headings on
60 beams, 230 000 gathers), take the best one or two cells *outside* 0.5 m
and 15 degrees of the current pose, refine each on the sharp grid as the
search does, and run the ray-cast judge on the result. A **rival** passes,
has no more through than the current pose, and beats it by M in agree. A
scan with a rival counts as a failed scan, on agree. This is the one case
the shares alone cannot see: a robot slipped a metre along a hallway has
the side walls agreeing at every position and only a sliver of end wall
disagreeing, no wider than an open door, so no bar on the shares separates
it from a true pose; but the true pose a metre away agrees on the end wall
too, and wins. A plain featureless corridor has no winner, both poses
score alike, and the robot rightly stays where odometry put it. The
metre-off recording is M's ceiling: M must sit below the agree gain the
true pose shows there. Whole judge, score plus rival check: under 50 ms a
tick on the Pi.

**Everyday life must pass.** A person walking past is short beams, counted
and tolerated; an open door is a through patch, and T must sit above the
highest open-door share in the drive-past recording and below the wrong
room's. A person standing a metre in front covers about a tenth of the
beams, all short, and at a low-agree spot that can push agree under A;
phase 1 records that case at the weakest cross, and if it fails, agree is
graded over counted beams minus short ones whenever through ≤ T (a crowded
true pose has low agree and low through; wrong poses have through). Phase
1 measures whether plain shares separate everything; the patch rule on the
shelf is the fallback, and if neither separates the wrong room and the
counter from true poses, this plan stops and is rethought rather than tuned.

**The state machine** runs on every tick in localize mode and owns the only
`localized`-like word anyone else reads. One count N (about four, from
recordings) drives it:

- `tracking`: the pose has passed N consecutive verdict scans since it was
  handed over or since the last `stale`, and the robot has moved at least
  0.3 m or 0.3 rad since then (a stationary robot can confirm nothing;
  prism_loc's gate, a design constant). It stays `tracking` through odd
  failing scans; see "dropping a pose" below.
- `checking`: a pose (boot, a search result, the first scan after `stale`)
  that has not yet earned `tracking`. **Clean** once a verdict scan has
  passed since it was handed over or since `stale`, and the latest verdict
  scan passed. A clean `checking` may start or release a trip, and the
  trip's first half metre is the confirmation; the tick judges before it
  releases. A running trip continues through `checking`. A `checking` pose
  that fails N consecutive verdict scans, in any way, is dropped.
- `searching`: the pose was dropped, or there is no pose at all (after a
  reboot). A running trip has been stopped and held; new trips are held.
  A new search starts on the first tick after the last one finished, so
  about once a second, on the latest non-turning scan. A clear winner is
  committed and enters `checking`. The reason for the debug panel: "nothing
  passes", "several candidates", "nothing found yet".
- `stale`: no fresh scan (the bridge's existing `scanning()` is false),
  whatever the pose. The other four states exist only while scanning. The
  pose, if any, is kept and shown softly; the first scan after the wake
  word or boot enters `checking` (pose) or `searching` (none).
- `off_map`: 20 s of searches in a row with no candidate passing on any
  map (20 s so it lands inside a held trip's 30 s deadline, whatever the
  search takes). The search keeps running; a clear winner leaves for
  `checking`.

**Dropping a pose.** A pose fails a verdict scan when it does not pass it or
when the scan has a rival. After N consecutive failing scans:

- The pose is dropped: `searching`, and a running trip is stopped and held.
  This is a wrong room, a wrong heading, a drag, a carry with the lidar on,
  the counter, an unmapped room, a hallway slip, a wrong seed.
- **The one exception, forgiveness.** If the pose was `tracking` when the
  streak began, the streak began directly after a passing scan (no `stale`
  or no-verdict gap), and all N scans failed on through alone (agree ≥ A,
  no rival), then the robot has not moved and the map is wrong where it
  stands: a removed coffee table, a sofa moved away. One search runs. A
  passing candidate that beats the current pose by M in agree is seeded as
  usual (the rival rule; a map change alone never moves the pose). If none
  does, the pose stands and stays `tracking`, through is forgiven for it
  until it next passes or fails on agree, forgiven scans keep the streak
  alive but earn nothing, the learner may vote from it while its agree is
  at least A + M, and the search is not re-run for it. A `checking` pose is
  never forgiven: a wrong room handed at boot fails on through and is
  dropped like anything else.

Why this is enough. A contradiction fails N scans in a row at once and goes
straight to `searching`; a wrong seed fails the same way. A symmetric
hallway driven from a known pose is neither: the side and end walls agree,
nothing goes through, no rival wins, and the robot stays `tracking` on
odometry. Floor run 1's lesson, never re-seed a tracking AMCL mid-drive, is
built in: the search commits only over a pose that has failed, and over a
forgiven one only when the newcomer is better by M. A carry with the lidar
on is expected to fail during the carry itself (turning scans are skipped,
the rest see over the furniture to walls behind), which also breaks any
streak that could be forgiven; phase 1 records one in a furnished room and
a bare one to check, and if either slips past the judge the anchored scan
check comes off the shelf. One residual case is accepted: a `tracking`
robot carried, lidar on, to a wall-aligned look-alike spot where no
candidate anywhere is clearly better would be forgiven there until it
drives somewhere that fails on agree.

What each state allows:

| state | trips | gamepad | emotes, in-place | barriers | save place |
| --- | --- | --- | --- | --- | --- |
| tracking | yes | yes | yes | from the pose, plus sensors | yes |
| checking | start or release when clean, else held; a running trip continues | yes | yes | from the pose, plus sensors | no |
| stale | held until the first passing scan | yes | yes | from the last pose, plus sensors | no |
| searching | held; a running trip is stopped and held | yes | yes | from TF's pose, plus sensors | no |
| off_map | refused, with the one sentence | yes | yes | sensors only | no |

Emotes hold the nav lock for two seconds, as today, and the nav lock ends a
held trip exactly as it ends a running one. Save place is refused in every
state but `tracking` with one text beside the button, "drive me a little
first". Barriers come from whatever pose TF has in every state but
`off_map`: a soft or doubted pose is still the best guess, the barriers
mark the steps (the floor test plan's check 30 fix, kept), and the
docstring's rule stands, a wrong stop over no stop. `off_map` is the one
exception because a robot in a genuinely unmapped room would otherwise stop
at phantom steps for as long as it stayed; the cost is that a false
`off_map` loses map barriers until the next find, with the cliff and
forward sensors still guarding. One more accepted wrinkle: a robot carried
while asleep publishes barriers from its old pose until the first search
commits, so a robot that was parked by the steps may stop once on its first
gamepad push in the new room; it clears itself within seconds.

Bars (A, T, M, the tolerance, N) are read off the recordings in phase 1,
not guessed, and stored in `robot_nav.py` next to the pure functions with
the recorded evidence noted in the comment, as `SCAN_FIT_*` is today. The
geometric constants (the third for no verdict, the 0.3 m / 0.3 rad gate,
the 0.5 m / 15 degree rival distance, the 1.5 m / 30 degree prior and rival
window) are design choices, checked against recordings, not derived from
them.

### 3. Search: whole map, one scan, no spin

A RPLidar scan already covers 360 degrees. Spinning the robot adds no
information; it only gave AMCL's particles time to collapse. The search
instead scores one scan at every pose on every saved map:

- Precompute per map one **score grid** from distance to the nearest wall:
  1 at the wall falling linearly to 0 at 30 cm, computed by the ring
  dilation `scan_fit` uses today, so the distance is quantised to cells.
  The **sharp** grid is that score at 5 cm; the **coarse** grid is its max
  over a 13 cell (65 cm) window. Built at bridge start for every PGM on
  disk, and again for a map the learner rewrites.
- Coarse pass: a plain grid of poses every 20 cm over the map's free cells
  dilated by one grid step (so a robot standing where the map drew the old
  sofa is still found; only wall cells are excluded), and every 3 degrees of
  heading, scored by summing coarse-grid values at the end points of every
  sixth beam (60 beams), dropping beams longer than 4 m. The numbers fit
  together by Hess's step rule, with the slop pooled instead of enumerated:
  a 20 cm grid puts the true pose up to 14 cm from a grid pose, a 3 degree
  step moves a 4 m beam end up to 10.5 cm at the half step, cell
  quantisation adds 5 cm, and the 32.5 cm half-window covers the sum, so
  the true pose's cell cannot be lost to pooling. About 40 m² of free floor
  is 1000 poses, times 120 headings, times 60 beams: 7 million gathers,
  chunked per heading, expected 0.2 to 0.6 s on the Pi; measured in phase 2
  against the one-second-per-map bar. If it misses, the knobs in order:
  every twelfth beam, a 3 m cap, the FFT pass on the shelf. No ray casting
  here: ray casting every grid pose is billions of samples.
- Keep the top twenty distinct peaks by non-maximum suppression in score
  order: a kept peak suppresses every cell within one grid step and one
  heading step of it, and suppression does not chain, since max-pooling
  turns each real mode into a small plateau of equal cells. Refine each on
  the sharp grid over ±35 cm and ±5 degrees at 5 cm and 1 degree on every
  third beam (120): about 2500 poses times 120 beams times 20 peaks, 6
  million gathers. Per axis, one grid step plus the half-step offset is 30
  cm inside the 35, and one heading step plus its half is 4.5 degrees
  inside the 5, so the true pose is inside the window of the peak that
  absorbed its cell. Then run the ray-cast judge on the twenty survivors,
  all 360 beams. Twenty is a start; replay confirms it keeps every labelled
  true pose, and if one ever drops out of the coarse top twenty the coarse
  through term comes off the shelf. The coarse score is the same "end
  points near walls" idea as today's defect, so the true pose need not be
  the coarse winner; the judge decides, by agree and through shares, never
  by raw coarse score, and the same shares rank candidates across maps.
- **Rules, written as formulas.** A candidate **passes** when agree ≥ A and
  through ≤ T (the judge's own bars). The **winner** is the passing
  candidate with the highest agree, and it is **clear** when every other
  passing candidate is at least M behind it in agree or has more through.
  M starts at 0.1, a plain guess, read off the recordings with the
  metre-off case as its ceiling; it is the same M the rival check uses.
  The **prior** is the map-to-odom transform frozen at the last `tracking`
  tick with live odometry added since; a candidate is **inside** it within
  1.5 m and 30 degrees. Inside candidates are judged first: an inside clear
  winner wins. An outside candidate must be clear against every inside
  passer too; a carried robot's inside candidates all fail, so the far one
  wins at once. With no prior (reboot, never yet `tracking`) the plain
  clear rule applies. In the forgiveness search the current pose is itself
  a candidate: a winner must beat it by M in agree.
- **No carried state, no lift sensor, no detector.** Arlen: "Do I know
  where I am? Is there another point that makes more sense? covers drag,
  carry, everything." It is one search, not two: the whole map is always
  scored, the prior only decides how much evidence a candidate needs, so a
  carry costs nothing extra. A seed from a mid-air scan enters `checking`
  and must pass a verdict scan before any trip is released on it; the
  wheels do not turn in the air so it cannot earn `tracking`, and the next
  search overwrites it when it fails.
- **Believe, then confirm by motion.** A clear winner is committed at once:
  seed AMCL (transported, part 1), enter `checking`; `tracking` follows N
  passes and the 0.3 m / 0.3 rad gate. If no winner is clear the state
  stays `searching` ("several candidates"), the trip stays held, and the
  search runs again a second later; as the robot moves, one fresh scan from
  a new spot breaks the tie. No candidate tracking across scans, no nudge:
  the robot never moves itself to localize. A tie nothing resolves runs out
  the held trip's 30 s and the give-up is spoken; the caption already says
  a nudge helps.
- **Which map.** The search runs on every saved map, read from the PGM
  files on disk (`/map` carries only the loaded one), and the winner names
  the map. A switch needs two consecutive clear wins on the other map (a
  mid-air scan during a carry must not reload the map and costmaps every
  second). The bridge asks map_server to load the other file with the
  asynchronous service call, so the tick never blocks; the judge is muted
  from the request until the new `/map` has arrived at the bridge and AMCL
  has been re-seeded (a map load leaves AMCL's particles where they were,
  which on the new map is simply a wrong pose). A learner reload of the
  same map needs no re-seed: the origin never moves, so the particles are
  still right; only the mute applies.
- **Not on a map.** 20 s of searches with nothing passing on any map:
  `off_map`, as above.

Where it lives: one `threading.Thread` per search, started from the tick
when the state is `searching` or `off_map` and no search is running (or
once, for a forgiveness search), writing one result attribute that the
next tick reads. Its own flag, not the bridge's `busy` activity lock that
relocalize uses today: an activity refuses trips, pauses the state machine
and resets the pending goal's clock every tick, all of which the trip rules
forbid. No queue, no result class, no localizer object; the maths is pure
numpy functions in `robot_nav.py` (or a sibling module if it outgrows the
file) with tests on synthetic and recorded scans. The search takes up to a
second and numpy releases the GIL for its big gathers, but whether the tick
and the barrier publisher stall is measured in phase 2; if they do, the
search moves to a subprocess.

**Held trips** reuse the pending slot the bridge already has
(`goal_request`, with `GOAL_START_SECONDS` = 30 s): the goal dict gains a
`state` of `held` and a `held_at`, and a dropped pose writes the running
goal back into that slot instead of cancelling it. `held_at` is set the
first time a trip is held (at the request if the robot is not ready, else
at the stop) and never reset, so one 30 s deadline covers all of a trip's
holds together; lidar spin-up counts against it, as it does today. One
consequence, intended: a long trip held 5 s at its start and dragged a
minute later has 25 s left to re-find and resume. The trip keeps its one
serial, the one in the reply, for its whole life; a release is a new Nav2
goal under the same serial, and the bridge tells a late result from the
stopped Nav2 goal apart by its goal handle, so it cannot kill the resumed
trip. `held` counts as in flight everywhere `navigating` does: "stop", a
gamepad twist and the nav lock cancel a held trip (the person took over;
they can ask again), and the voice's trip tool keeps polling. When the
deadline passes while the hold is the localizer's (`searching`, or a
`checking` not yet clean) the goal ends `failed` with reason `lost`; a
hold that ran out waiting for the lidar or Nav2 ends with today's reasons,
unspoken; when the state reaches `off_map` a held goal ends at once with
reason `off_map`. The deadline is what bounds the robot stuck on a rug:
wheels turn, odometry advances, the world does not, which reads as a
dropped pose, a re-find at the true pose and a resume into the same rug;
without the deadline that loop is silent and endless.

**The voice** has no standing poll today: the trip tool returns the moment
the bridge accepts, and the goal is looked at again only if the model
chooses to call the check tool. So a held trip is the tool's to wait out.
When the bridge's reply carries `accepted: true`, the goal's serial and
`held: true`, the trip tool keeps polling `nav_status` inside the call
until the trip leaves, is cancelled or fails (bounded by the 30 s
deadline), speaks the canned lines from that poll through the runner's
progress speaker threaded into the tool context, and only then returns to
the model. What the model sees is a normal trip in flight, or `failed`,
with `spoken: true` when a line was said and a note, "already told the
user; say nothing about where the robot is"; never a reason word about
position, never a line's text. A refused trip in `off_map` carries the same
note. A repeat request during the same hold is `accepted: true` with the
same goal dict, not a refusal. `where_am_i` keeps its boolean and a canned
note, "the robot sorts this out itself, just ask for the trip", never the
state word. The trip tool's description loses "spins in place to find
itself" and gains one sentence: the tool speaks for itself about position.
The three lines, spoken verbatim and only in these cases:

- "Hang on, I'm working out where I am." Once per trip, from the poll, when
  the trip has been held with the state `searching` for about 3 s. A hold
  in `stale` or `checking` (the lidar still spinning up after an ordinary
  idle) and a hold the search clears in a second say nothing. If this line
  is ever heard after an ordinary idle, that is the pre-trip pause leaking
  through the speaker, and the fix is the lidar rules, not a nicer sentence.
- "I've lost track of where I am. Move me a little and ask again." Once,
  for the serial this tool started, when its poll first sees that goal
  `failed` with `lost`. A give-up after the tool has returned (a drag after
  the trip left, the model not polling) is silent until the UX audit's
  cluster A (voice deaf during trips) gives the robot ears during trips.
- "I can't work out where I am from here. Put me on the floor somewhere I
  know." When a trip is asked for in `off_map`, or the poll sees its goal
  end `failed` with `off_map`; never unprompted.

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
- **Votes**, only while `tracking` (a forgiven pose votes only while its
  agree is at least A + M), within 4 m (where 1 degree beams are still
  closer together than two cells), hitting the wall at more than 20 degrees
  from grazing. A through beam votes free on the wall cell it crossed; a
  short beam votes occupied where it stopped; an agreeing beam votes *for*
  the wall cell it hit, which can only block or reset a change, never make
  one. That is what stops the learner eating itself: a pose 10 cm off for a
  whole lap casts only confirming votes, because 10 cm is inside the agree
  tolerance.
- **Visits and verdicts.** Votes on a cell until it has had none for 30
  minutes are one visit; a visit's verdict is clear at 20 votes, 90 percent
  one way; a cell changes after three visits against it with none for it in
  between (defaults accepted by Arlen). Both gates are at least as strict
  as the clamped log-odds update lifelong mappers use.
- **Reload.** The bridge asks map_server to reload by the `load_map` service
  it already uses, asynchronously, judge muted until the new map arrives,
  then rebuilds the score grids. No re-seed: same map, same origin.

### What this gives Arlen's four points

1. No spin: one scan, whole map, candidates in under a second, confirmed by
   the first half metre of the trip or whatever drive is already happening.
2. Continuous: the judge runs every tick in every mode of driving; a pose
   that fails N scans, or has a better rival nearby, starts a search without
   a button, weighed from where odometry says the robot should be. A
   symmetric hallway driven from a known spot disturbs nothing.
3. Which map: the search scores all maps and picks the one that survives.
4. Accurate: nothing is `tracking` without a high agree share, a low through
   share, no better pose nearby, and motion the judge agreed through; AMCL
   is configured so it can actually hold 10 cm. A 55 percent match of any
   kind is `searching`, and the marker says so.

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
- **Shares alone, no rival check.** A shift along a corridor is invisible to
  any single-scan score; asking "is there a better pose nearby" is the
  only honest answer, and it is a few milliseconds of the search's own
  refine step.
- **"Once TF has landed on the seed" as the rule for when verdicts count.**
  A moving robot's pose never lands anywhere; reading the latest
  `map -> odom` makes the first scan after the seed count, no rule needed.
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
- **A resume count** on held trips. One deadline bounds the same loop with
  one number fewer.
- **Removing the pre-trip spin in phase 1.** Without the search a lost
  robot's trip would sit silent for 30 s across five evenings; the spin
  stays, judged strictly, until the search replaces it.

On the shelf, one line each, built only when the named recording shows the
simpler thing failing:

- **Patch rule**: group through beams into angular patches, drop the largest
  one or two, count the rest; if the open-door recording's through share
  nears the wrong room's.
- **Edge rule**: ignore beams predicted to hit within two cells of a wall end
  or an unknown cell; if true-pose through shares stay noisy after the
  neighbour rule.
- **Short-blind agree**: agree over counted beams minus short ones when
  through ≤ T; if the standing-guest recording drops agree under A.
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

### Phase 1: the judge (five evenings)

- A `record` command in the bridge, with a label and an optional `seconds`,
  saves the latest scan, the judged map pose, AMCL's pose and odometry to a
  JSON lines file under `~/.config/robot-pet/recordings/`; with `seconds` it
  appends every tick's scan for that long. A button in the debug panel sends
  it. Truth comes from three or four taped crosses, each tape-measured from
  two mapped walls with a taped heading line; a recording at a cross
  carries that pose as its label, elsewhere Arlen's sentence is the label.
  Tonight's wrong room and the metre-off pose are the first two entries.
  Further recordings: true pose at the crosses and the places, right spot
  wrong heading, on the counter, on the rug (tilt lets beams see over low
  furniture), a carry with the lidar on through a furnished room and a bare
  one, a drag along the hallway, a gamepad drive around the floor while
  correctly localized (as a sequence, for the motion gate and AMCL's
  drift), a drive past the open door and the dark sofa, a person walking
  past, and a person standing a metre in front for 30 s at the
  weakest-scoring cross.
- `tools/localization_replay.py` prints today's `scan_fit` and the new
  `agree` and `through` shares for every recording, AMCL's error at each
  cross, and which angular sectors are always short (the robot's own body,
  masked once). That table goes into this document and A, T, the tolerance
  and N are read off it, with margin. Pass for the judge: every true pose
  (the open-door, rug, walking-past and standing-guest recordings count as
  true) passes, and the wrong room, the wrong heading and the counter fail,
  with daylight between. The metre-off recording is reported, not required:
  if it passes on shares, that is the rival check's job in phase 2, not a
  defect. If the wrong room or the counter cannot be separated, the patch
  rule comes off the shelf, then the edge rule; if the standing guest fails,
  short-blind agree; if nothing separates the wrong room and the counter,
  this plan stops and is rethought rather than tuned. The carry recordings
  decide the anchored check; the cross recordings decide the tracker.
- Map audit: the replay draws where through beams land at the known-true
  poses. Clusters on a few cells are map errors (diagonal walls with corner
  gaps included): fix those cells or re-map (doors in their usual state,
  nobody walking, slow drive); if unknown speckle inside rooms swallows
  beams, fill unknown islands that do not touch the border at map load.
  Spread everywhere means the tolerance is wrong, not the map. The verdict
  on `groundfloor` goes into this document before any bar is set.
- `robot_nav.beam_check(...)`: the ray cast at half-cell steps, the
  five-beam neighbour rule, no-return and unknown as no opinion, the
  no-verdict rule; pure numpy, tests on synthetic maps and on the
  recordings, which run in the normal suite. The judged pose is the latest
  `map -> odom` composed with `odom -> laser` at the scan's middle.
- The state machine in the bridge with four states, `tracking`, `checking`,
  `searching`, `stale`, one count N, the motion gate, clean after one pass,
  the tick judging before it releases, dropping after N failures with
  forgiveness for a `tracking` pose only (no search yet to ask, so it
  simply forgives). `localized` becomes "tracking or clean checking".
  Entering `searching` cancels a running trip through the cancel path that
  already exists. Save place needs `tracking`, with its refusal text beside
  the button. The pre-trip spin and the boot spin stay in this phase, but
  `try_map` accepts a spin when `beam_check` passes the scan at AMCL's pose
  instead of on `scan_fit`, so a wrong room is never accepted; a trip whose
  spins all fail ends `failed` with reason `lost` (today's `not_localized`,
  renamed once). `scan_fit` and its bars are deleted; the turn-gate
  constant and the ring dilation stay, the judge and the search use them.
  "Worse" in this phase means a wrong room accepted, not fewer trips
  started.
- AMCL: `update_min_d` and `update_min_a` 0.1, `sigma_hit` 0.1, seed
  covariance 0.02 and 0.01, seed transported by the bridge and stamped with
  the newest odometry time.
- Voice: the trip tool's description loses "spins in place to find itself";
  a `failed` result carries the "already told the user" note in place of a
  reason word.
- Dashboard: the marker (solid, soft, hollow, none) and the caption in
  front, with the `searching` caption "lost, press Localize" until phase 3;
  state word, shares, reason text, map name behind the debug toggle.
- Exit, on the floor: the wrong room and the wrong heading are never shown
  as `tracking`, and a spin from the wrong room is never accepted; a
  correct pose reaches `tracking` within N scans after a seed and a short
  drive; the counter drops to `searching` within N scans; a hand-dragged
  robot drops to `searching`; a robot parked nose to a wall still leaves on
  a trip on its first verdict scan; a robot carried while asleep shows
  `stale`, never a solid marker in the old room, and never drives off on
  the old pose; Save place is refused with its text until `tracking`; a
  correctly localized robot driven by gamepad through the hallway and into
  corners, past a moved chair, an open door, over the rug, past the dark
  sofa and past a person standing in front, stays `tracking` the whole way;
  AMCL's error at the crosses is written down.

### Phase 2: the search and the rival check (four evenings)

- `robot_nav.global_search(...)`: the score grids for every PGM, the coarse
  pass at 20 cm and 3 degrees on 60 beams capped at 4 m, non-maximum
  suppression, top twenty, sharp refinement over ±35 cm and ±5 degrees on
  120 beams, judge on the survivors, the pass/clear/prior formulas, the
  current pose as a candidate in a forgiveness search. Timed on the robot
  against the one-second-per-map bar, and the tick and barrier publisher
  watched for stalls while it runs. Tests on recordings: every labelled
  true pose is in the coarse top twenty and is the clear winner after the
  judge; the wrong room recording produces no passing candidate at the
  wrong room; the counter recording produces none.
- `robot_nav.rival_check(...)`: the pooled-grid neighbourhood, the best one
  or two outside the exclusion refined and judged, run by the judge every
  verdict scan; a rival is a failed scan on agree. Test: the metre-off
  recording fails by rival, and sets M's ceiling; the gamepad-drive
  sequence never has one.
- The forgiveness path now runs its one search before forgiving.
- Relocalize becomes "search now" on the thread, not an activity: the
  Localize button drops the pose to `searching` (cancelling a running trip,
  as in phase 1) and searches; the pre-trip spin, the boot spin and
  `RELOCALIZE_SPINS` are deleted. A trip pending in `searching` re-runs the
  search every second until it leaves or its 30 s pass, so a voice trip from
  a lost robot works in this phase and a tie is broken by whatever motion
  happens. The button stays in front only until phase 3 gives the search
  its own trigger.
- All saved maps are searched; the winner names the map after two
  consecutive clear wins; the map load is asynchronous and the judge is
  muted until the new map arrives and AMCL is re-seeded.
- AMCL `recovery_alpha_slow` and `recovery_alpha_fast` to zero; the search
  owns recovery now.
- Exit, on the floor: carried to twenty spots, set down, Localize pressed:
  the right candidate within 5 s where the spot is unambiguous, `tracking`
  within the first half metre of a trip, never a wrong room, zero misses (one
  miss is a bug to find in the recordings, not a statistic); at the crosses,
  within 10 cm and 5 degrees; a search on the counter stays `searching`,
  never committed; a robot slipped a metre along the hallway drops to
  `searching` within N scans and re-finds the true pose on the next trip
  request; a tie (the two matching corners of the living room, if there are
  any) breaks within a metre of gamepad driving while a trip is pending;
  with a second saved map (map one other room for the test), carry the
  robot onto it: the map switches after two clear wins and a trip to a place
  on it runs.

### Phase 3: always on (three evenings, one exit blocked)

- `searching` runs the search on its own, one after another; boot with no
  remembered pose searches too.
- The pending goal grows its `held` state and `held_at`; a dropped pose
  moves the running goal into it instead of cancelling; a release is a new
  Nav2 goal under the same serial; `held` counts as in flight for stop,
  gamepad, nav lock and the voice's poll; the trip leaves on its own from
  `tracking` or a clean `checking`. The reply carries the serial and
  `held`, or the refusal reason; the give-up ends the goal `failed` with
  `lost` only when the hold was the localizer's, `off_map` ends it with
  `off_map`. The voice's trip tool waits out a hold inside the call,
  speaks the three canned lines through the runner's progress speaker,
  shows the model a normal trip or `failed` with `spoken: true` and the
  note, and answers a repeat with the same goal; `where_am_i` gains the
  canned note.
- `off_map` after 20 s of searches with nothing passing; a held goal ends
  `off_map` on that edge; barriers from sensors only in `off_map`. The
  Localize button moves behind the debug toggle, with the candidate list.
- Dashboard: the goal row shows "held"; a refused trip from a map click
  shows in the goal row, not the log; the `searching` caption becomes
  "finding my place, a nudge helps"; the `off_map` caption points at the
  existing Start mapping button, no new button.
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
  Carry it to another room: same, minus the resume. Say "stop" during a
  hold: the trip is gone and nothing resumes. Ask twice during a hold: one
  "hang on", nothing else. Put it on the counter and ask for a trip:
  `searching`, "hang on" at 3 s, `off_map` at 20 s with the off-map
  sentence once, no trip, no lost line. Lidar off, carry it, wake word:
  `stale`, `checking`, a search, never the old pose shown as tracking.
  Park it on the rug so it slips: stops and resumes end with the lost line
  inside the 30 s deadline, not silence. Remove the coffee table: the robot
  stays `tracking`, trips run. A drive past the dark sofa and through the
  hallway at speed never stops a trip. A gamepad push during a hold ends the
  trip and the goal row says so. Nothing about position is ever said twice
  unprompted.

### Phase 4: the learner (re-planned at phase 3 exit; about three evenings)

- `robot_nav.map_votes(...)` (votes from one scan's beams at a tracking
  pose, reusing the judge's ray cast) and `robot_nav.apply_visits(...)`
  (visits, verdicts, the three-visit rule). Tests on synthetic maps: a
  removed box fades after three visits, not two; a wall under a constant 10
  cm pose offset never moves; a door seen open twice and closed once stays;
  nothing changes from a `checking` or `searching` pose, nor from a
  forgiven pose under A + M.
- The learner runs in the bridge, writes the PGM and the evidence file
  atomically when idle, reloads with the judge muted, rebuilds the score
  grids. The pristine copy is made on first run if missing.
- Map card: "forget what you learned", with a count, shown only when there
  is something to forget; the change overlay is in the debug panel only.
- Exit, on the floor: remove the coffee table; after three visits on
  separate occasions it is gone from the map and the judge's through share
  there is back to zero. Localization accuracy is unchanged across the
  phase; the map's origin, places and barriers are untouched.

Total: twelve evenings for localization that just works, plus about three
for the learner.

## Dashboard words

One marker and one caption per state, in front of the debug toggle:

| state | marker | caption |
| --- | --- | --- |
| tracking | solid | none |
| checking | soft | "checking my position" |
| searching | hollow, at TF's pose | "finding my place, a nudge helps" (phases 1 and 2: "lost, press Localize") |
| stale | soft, at the last pose, or none | "lidar off" |
| off_map | none | "can't find my place here. Put me somewhere I know, or start mapping below." |

Save place refusal beside the button: "drive me a little first". Goal row:
"held", and "cancelled (gamepad)" when a push ended a hold. Behind the
toggle: state word, agree and through shares, the reason text, the
candidate list, the Localize button, and the learner's change overlay. A
robot that wakes and is never driven stays `checking` honestly; the motion
gate is what "confident" means.

## Decisions

The settled list; the numbers live in The design, once each. The dated
history of how each was reached is in git
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
- The lidar starts on the wake word; only the lidar. It stays on a minute
  after a conversation (part of the lidar power plan, noted here).
- The gamepad is never slowed or blocked.
- The Localize button lives behind the debug toggle once the search runs on
  its own.
- Map learning is part of the design: one map changed in place, no learned
  copies or versions; a hidden mapping-day original and "forget what you
  learned" as the correction; three visits, 30 minute visit gap.
- No separate measuring evening; recordings come from normal floor tests.
- Dashboard words in front are Claude's call.

Claude's, as final arbiter after seven review rounds:

- AMCL stays the tracker, with tighter update distances and a sharper beam
  weight as trials; the refine-step tracker is on the shelf, decided by
  AMCL's measured error at the crosses. The bridge transports the seed
  forward by odometry itself. The judge reads AMCL's latest belief, not the
  belief at the scan's time.
- One count N and one margin M. A pose is dropped after N failing scans;
  the one exception is a `tracking` pose failing on through alone directly
  after a pass, which asks the search once and is forgiven only if nothing
  beats it by M. Through is claimed only against the longest neighbouring
  prediction; agree against any. The rival check runs every verdict scan
  on the pooled grid, judging the best outside the exclusion.
- `tracking` is not lost to a single bad scan; `checking` is never
  forgiven; clean needs one passing verdict; `stale` means no fresh scan
  and ships in phase 1; `off_map` is a time, not a count, so it beats the
  trip deadline.
- No-verdict scans count for nothing anywhere.
- The search is a thread in the bridge with its own flag, one result
  attribute, not an activity and not a unit; held trips reuse `goal_request`
  with one `held_at` per trip and no resume count; the serial never changes;
  `held` counts as in flight everywhere `navigating` does.
- The coarse pass and refine numbers are as in part 3; peaks by
  non-maximum suppression; a map switch needs two consecutive clear wins
  and an asynchronous load; a learner reload re-seeds nothing.
- The trip tool waits out a hold inside the call and speaks the three
  canned lines itself; the model sees a normal trip and a "say nothing"
  note.
- Barriers from TF's pose in every state but `off_map`, for the reason
  written in part 2.
- Marker plus one caption per state in front; everything else behind debug.
- Phase 1 ships four states and keeps the spins, judged strictly; phase 2
  deletes them with the search and zeroes AMCL's recovery; `off_map`
  arrives with its trigger in phase 3.
- Learner votes from every beam within 4 m at a tracking pose, confirming
  votes included, never from a forgiven pose under the clear bar;
  "sometimes" cells on the shelf; built last, re-planned at phase 3 exit.

## Sources

- [Olson 2009, Real-time correlative scan matching](https://april.eecs.umich.edu/pdfs/olson2009icra.pdf), [Olson 2015, M3RSM](https://april.eecs.umich.edu/pdfs/olson2015scanmatch.pdf)
- [Hess et al. 2016, Real-Time Loop Closure in 2D LIDAR SLAM](https://research.google/pubs/pub45466); [Cartographer pose_graph.lua](https://github.com/cartographer-project/cartographer/blob/master/configuration_files/pose_graph.lua) for the 0.55 / 0.6 bars
- [prism_loc, 2D MCL with branch-and-bound global relocalization and verification](https://github.com/kjungmo/prism_loc), [parameters](https://github.com/kjungmo/prism_loc/blob/main/PARAMS.md)
- [Nav2 AMCL configuration](https://docs.nav2.org/jazzy/configuration_and_development/configuration_guide/others/configuring_amcl/), [ReinitializeGlobalLocalization BT node](https://docs.nav2.org/jazzy/configuration_and_development/configuration_guide/core_servers/bt_plugins/actions/ReinitializeGlobalLocalization/), Nav2 `nav2_amcl/src/amcl_node.cpp` (`handleInitialPose`, transform post-dating, `handleMapMessage`) and `sensors/laser/likelihood_field_model.cpp` (beam weights summed)
- [Beluga AMCL](https://github.com/Ekumen-OS/beluga), [ROSCon 2024 slides](https://roscon.ros.org/2024/talks/Beluga_AMCL-_A_modern_Monte_Carlo_Localization_implementation_for_ROS.pdf)
- [slam_toolbox README](https://github.com/SteveMacenski/slam_toolbox), [JOSS paper](https://joss.theoj.org/papers/10.21105/joss.02783.pdf)
- [Cartographer tuning](https://google-cartographer-ros.readthedocs.io/en/latest/tuning.html)
- [Thrun et al., Robust Monte Carlo localization (Mixture MCL)](https://www2.cs.sfu.ca/~vaughan/teaching/415/papers/thrun_robust.pdf); Thrun, Burgard, Fox, Probabilistic Robotics, ch. 6.3 (beam model) and ch. 8.3.5 (Augmented MCL)
- Lenser and Veloso 2000, Sensor Resetting Localization; summarised in [Sehestedt and Schneider, MCL in highly symmetric environments](https://www.scitepress.org/Papers/2006/12158/12158.pdf)
- [Dreame multi-floor guide](https://www.dreametech.com/blogs/blog/multi-floor-mapping-robot-vacuum), [Valetudo multi-floor thread](https://community.home-assistant.io/t/multi-floor-vacuuming-using-valetudo-and-dreame-robots/933706)
- [Walcott-Bryant et al., Dynamic Pose Graph SLAM](https://people.csail.mit.edu/kaess/pub/WalcottBryant12iros.pdf), [Zhao et al. 2021, lifelong localization and mapping framework](https://arxiv.org/pdf/2111.10946)
