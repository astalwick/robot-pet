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
score that counts beams passing *through* mapped walls) plus a **search**
(one 360 degree scan matched against the whole of every saved map by
brute-force correlation, no spinning, running the whole time the lidar is
on) plus, once those three hold on the floor, a **learner** that keeps the
one map true as the house changes. Arlen's two questions are asked
literally, every second: the judge asks "does this pose contradict the
map", the search asks "is there another spot that makes more sense", and
nothing is believed until a little motion confirms it. Those two questions
cover drags, carries, heading flips, hallway slips and reboots alike. No
lift sensors, no carried state, no detection of anything, no special cases.
From Arlen's chair the whole thing is invisible: the lidar wakes with the
wake word, a trip leaves without a pause or a wiggle, and the robot speaks
only when it has genuinely been moved somewhere it cannot place. One thread
in the nav bridge, one module of pure functions, one scan count, two
margins, five states, three phases plus the learner.

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
max-pooled coarse grid, expanding coarse cells in score order until no
unexpanded cell's bound can beat the best fine score found
([Olson 2009](https://april.eecs.umich.edu/pdfs/olson2009icra.pdf);
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
Robotics, the opening of ch. 6.4): a 2 degree heading error at 5 m moves a
beam 17 cm sideways, past a door frame, and reads as "through". The cure is
to compare against the predictions of neighbouring beams too, in the
direction that makes "through" harder to claim, not easier. And one class
of error no single-scan contradiction score can see: a pose that puts walls
at the right distances but in the wrong place, a shift along a corridor or
a half turn in a room whose furniture the lidar cannot see. Every such pose
agrees with the map; only a better pose elsewhere tells it apart. The
honest answer is to keep asking whether another pose fits better.

**Robot vacuums.** Public technical detail is thin; Dreame's marketing says
its X50 Ultra "recognises which floor it is on using landmark detection"
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
The standard practice for a grid that changes over time is the same two
gates: update the map only from poses the localizer itself accepts, and
update each cell slowly with clamped evidence, counting confirming hits as
well as misses, so one bad pass cannot flip it. That is what a vacuum
effectively does when it "adjusts" after you move a sofa.

## The goal

A robot that always knows where it is, without being told. It finds its
position on the map from where it stands, without spinning. It keeps
checking that position while it drives or is driven and fixes it when it is
wrong. It knows which saved map it is on. It never calls itself localized
when it is not. Carried, dragged mid-trip, rebooted: it works it out on its
own, and the dashboard tells the truth about what it believes. Later, the
map absorbs changes to the house, and the robot knows when it is on no map
at all.

One principle runs through the design: **the judge catches contradictions,
the search catches better answers, motion confirms.** A pose is dropped
when its scans contradict the map or when the search finds a clearly better
pose elsewhere; nothing is believed until a little motion confirms it. A
robot that knew where it was and drove down a plain hallway is not lost: its
scan agrees with the map, no other pose fits better than the one it drove
to, and it stays where odometry put it.

## Requirements

Arlen's, in his order:

1. Localizes without spinning in circles.
2. Keeps localizing while navigating or being driven, unless it is already
   confidently right.
3. Detects which map it is on.
4. Accurate above all: never says localized at a 55 percent match.

Later: learns changes to the map; knows when it is on no map at all.

Agreed with Arlen on 2026-10-27 (each is spelled out once, in The design):

- Everyday changes (a moved chair, an open door, a person, the dog, a
  removed coffee table) never cost `tracking`; the judge tolerates things
  closer than the map says, and a single patch of beams going through where
  the map drew something is "something changed", not a contradiction.
- The map is audited before any bar is trusted; unknown is never a wall.
- Position within 10 cm and heading within 5 degrees while `tracking`; a
  clear candidate within 5 s of the first scan where the spot is
  unambiguous; zero false `tracking` over twenty carry trials, one miss is a
  bug. Truth comes from taped crosses, for testing only.
- A trip waits for the robot, never the other way round: held, not refused,
  and it leaves on its own; a trip stopped by a lost pose resumes on its
  own; one 30 s deadline per trip bounds the waiting for the localizer.
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
9. **Fits the Pi 5.** A whole-map search, coarse pass to judged candidates,
   under two seconds per map; the judge under fifty milliseconds per tick;
   both in numpy (scipy is not on the robot).
10. **One count, two margins.** One number N of consecutive verdict scans
    decides when a pose is dropped and when it is earned. One margin M says
    when one candidate clearly beats another (rooms apart; from the
    wrong-room recording) and gives the search its commit bar. One smaller
    margin R says when a nearby pose is a rival (a hallway slip or a
    heading flip; from the metre-off recording). The judge runs on the
    bridge's 0.5 s tick, so N scans is N/2 seconds.
11. **Honest surface.** One thread in the nav bridge and one module of pure
    functions carry the maths. The plan also touches, and says so: the
    bridge (recorder, judge, state, search thread, held trips, reasons on
    replies), the voice (the trip tool waits out a hold and speaks three
    canned lines as the turn's own speech), the dashboard (marker, caption,
    debug panel, Localize button moved into it, Save place gated, held goals
    drawn), the replay tool, and AMCL's configuration (part 1). One outside
    dependency: the lidar power plan (not yet written) for the wake-word
    lidar. Nothing else in the stack changes.

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
  the cube of each beam's probability, so the beam count barely matters and
  `max_beams` stays), at the cost of faster depletion on map errors;
  `resample_interval` 2 if depletion shows.
- Seed covariance 0.1 m² and 0.07 rad² to 0.02 and 0.01: a 14 cm, 6 degree
  spread, a little wider than the search's own error.
- **Seed transport.** A search takes up to two seconds, in which a gamepad
  robot moves most of a metre, or two at turbo. AMCL's own seed handling
  cannot carry the pose forward: it looks the odometry up against its own
  clock with no timeout, our odometry is stamped with that same clock, so on
  a live robot the lookup always fails (the "failed to transform initial
  pose in time" warning seen on the floor) and AMCL plants the seed as the
  pose *now*. So the bridge does the carrying: the found pose is moved
  forward by the odometry between the scan's stamp and the newest
  `odom -> base_link` the bridge holds, and the seed is stamped with that
  newest odometry time. The error is one odometry period at 20 Hz, about
  2 cm.
- **Seed landed.** After a seed AMCL publishes exactly one `/amcl_pose` on
  its next scan, stamped with that scan's time (which is earlier than the
  seed's), and then nothing more until the robot moves; so the rule goes by
  arrival, not stamp: the first `/amcl_pose` the bridge receives after it
  published the seed, with a pose within 0.5 m of the seed (AMCL's first
  mean is already scan-corrected, so it will not sit inside the 14 cm
  spread), says AMCL took it, and verdict scans count from the next tick.
  If none arrives within two ticks the seed is sent again. This also covers
  the map switch below: AMCL range-checks a seed against the map it holds,
  and a seed that reaches it before the new map is dropped with an error.
- `recovery_alpha_slow` 0.001 and `fast` 0.1 to zero in the same phase that
  ships the search (phase 2): two recovery mechanisms fighting is the
  classic failure. From then on AMCL cannot recover from a wrong seed by
  itself; the judge and the search are the only way out, by design.

Accuracy while `tracking` is AMCL's: the judge's tolerance (part 2) is
wider than 10 cm on purpose, so a pose 25 cm off still passes the judge,
and the search's rival distance (0.3 m) is where a slip begins to be seen.
Whether AMCL holds 10 cm and 5 degrees is therefore measured, not assumed:
phase 1's recordings at the crosses carry AMCL's pose beside the true one.
If it holds, the tracker question is closed. If not, the scan-to-map refine
step from part 3 becomes the tracker (on the shelf), decided at phase 1
exit.

### 2. Judge: a strict score, a rival from the search, one count

**The pose it judges.** The lidar's map pose at the middle of the scan,
composed from the *latest* `map -> odom` (a single-edge lookup of that one
transform at time zero, AMCL's current belief; a lookup at the scan's own
time, or of the chained `map -> base_link` at time zero, returns the belief
from a second earlier because of the post-dating above) and `odom -> laser`
at the scan's stamp plus half its duration (at 0.25 m/s half a second of
lag is 12 cm). The driver is assumed to stamp a scan at its first ray, the
sensor_msgs convention; phase 1's first recording confirms it, along with
the revolution time: the lidar plan's 5.5 Hz at 8000 samples a second
gives about 1440 points in 182 ms, and those are the working numbers.

**The score** replaces `scan_fit`. The judge uses every fourth point, 360
beams at 1 degree spacing. For each, ray-cast across the map in half-cell
steps to the first occupied cell (a one-cell wall at an angle can still
show corner gaps; the map audit looks for through clusters along diagonal
walls). To blunt the beam model's brittleness in angle, each beam is
compared against the predictions of itself and its three neighbours on
either side (±3 degrees, seven predictions), in the direction that makes
"through" harder to claim. Three degrees because a scan taken at the turn
gate's limit skews ±2.1 degrees about its middle, and Nav2 turns under the
gate while path-following; the replay reports the shares at ±2 and ±3 so
the narrower rule can be chosen if it separates better.

- **agree**: measured within the tolerance of *any* of the seven
  predictions (20 cm plus five percent of range to start; read off
  recordings, and at least twice the tracker's measured error at the
  crosses, or an honest 10 cm tracker reads as unsure).
- **through**: measured more than twice the tolerance beyond the *longest*
  of the seven predictions, where that prediction is within 6 m. The beam
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
  open front door is uninformed, not lost. A scan taken while turning faster
  than the bridge's existing `SCAN_FIT_MAX_TURN_RADIANS` per tick is a
  no-verdict scan too, the turn read from odometry yaw rather than the map
  pose (a seed's heading correction must not read as a turn); Nav2 turns in
  place at 0.8 rad/s, so every such scan is skipped.

**The patch rule.** Before the shares are taken, the single largest patch
of through beams that is contiguous over counted beams (no-opinion beams in
between do not break it; the A1 drops returns exactly on dark furniture) is
dropped from the count entirely. A removed coffee table, a sofa moved away
or an open door is one patch of beams going through where the map drew
something, and that is "something changed", not a wrong pose. A wrong pose
sends beams through mapped walls in more than one direction, and those
remain. The dropped patch has no width cap to start; if the replay shows a
wrong pose passing only because its through sat in one patch, a cap comes
in, read off that recording. (The learner, part 4, is what eventually
erases the patch from the map.) Two numbers then come out of a scan with a
verdict: the `agree` share and the `through` share, both over the counted
beams less the dropped patch.

A pose **passes** a scan when agree ≥ A and through ≤ T. A wrong room
fails on through: it puts walls at plausible distances but sends beams
through mapped walls on several sides. From the counter, beams either run
over the mapped counter top to the far walls (through) or hit the cabinets
at wrong distances (agree low); either way it fails. An unmapped room is
short beams everywhere and low agree. What the shares alone cannot catch,
and are not asked to, is a pose that agrees with the map in the wrong
place: a metre's slip along a hallway, a half turn in a room whose
furniture the lidar sees past. Those have high agree and a through share
that one dropped patch can hide. They are the search's job, below.

**Everyday life must pass.** A person walking past is short beams, counted
and tolerated; an open door and a removed table are one dropped patch
each; T must sit above the highest through share left after the patch rule
in the drive-past and removed-table recordings and below the wrong room's.
A person standing a metre in front covers about a tenth of the beams, all
short, and at a low-agree spot that can push agree under A; phase 1 records
that case at the weakest cross, and if it fails, agree is graded over
counted beams minus short ones whenever through ≤ T (a crowded true pose
has low agree and low through; wrong poses have through). Phase 1 measures
whether these shares separate the wrong room and the counter from every
true pose; if they do not, this plan stops and is rethought rather than
tuned.

**The rival** is the search of part 3, which runs the whole time the lidar
scans, one search after another on every saved map, with the current pose
scored as a candidate alongside on the same scan. While the state is
`tracking` or `checking` its result is read for a rival: a candidate more
than 0.3 m or 10 degrees from the current pose that is committable (the
search's own pass rule, agree ≥ A + M, through ≤ T, two thirds of beams
counted), has no more through than the current pose, and beats it by R in
agree. A rival result **stands** against the pose until the next search
result replaces it: every verdict scan in between counts as failed on
agree, so N consecutive failures are reached in N/2 seconds (a result that
only marked one scan would be undone by the passing scan after it). This
is how the slip and the flip are caught: a robot slipped a metre along a
hallway has the side walls agreeing at every position and only a sliver of
end wall disagreeing, no wider than an open door; but the true pose a metre
away agrees on the end wall too, and wins by R. A robot turned half round in
a room agrees on the walls; the true heading also agrees on the sofa, and
wins. A plain featureless corridor has no winner, both poses score alike,
and the robot rightly stays where odometry put it. R comes from the
metre-off recording: below the agree gain the true pose shows there, above
the largest lead any other candidate ever has over the true pose along the
gamepad-drive recording. One residual case is accepted: a room symmetric
under a half turn, seen from a spot where nothing breaks the symmetry, has
two equal poses; only seeing its door or furniture tells them apart, so
there a trip from its middle may run out its deadline.

**The state machine** runs on every tick in localize mode and owns the only
`localized`-like word anyone else reads. One count N (about four, from
recordings) drives it:

- `tracking`: the pose has passed N consecutive verdict scans since it was
  handed over or since the last `stale`, and the wheels have rolled at
  least 0.3 m since then by odometry (a stationary robot can confirm
  nothing, and a robot turned by hand in the air confirms nothing either;
  prism_loc's gate, distance only, a design constant). It stays `tracking`
  through odd failing scans; see "dropping a pose" below.
- `checking`: a pose (boot, a search result, the first scan after `stale`)
  that has not yet earned `tracking`. **Clean** once a verdict scan has
  passed since it was handed over or since `stale`, and the latest verdict
  scan passed. A clean `checking` may start or release a trip, and the
  trip's first 0.3 m is the confirmation; the tick judges before it
  releases. A `checking` pose that has seen only no-verdict scans for 5 s
  (a design constant: a remembered pose facing the open front door) also
  releases a held trip; today such a pose leaves at once, and the trip's own
  motion brings the verdict scans that judge it. A running trip continues
  through `checking`.
- `searching`: the pose was dropped, or there is no pose at all (after a
  reboot). A running trip has been stopped and held; new trips are held.
  The tick does not judge TF's pose here; the search judges its candidates,
  the TF pose among them, and a clear winner is committed and enters
  `checking`. The reason for the debug panel: "nothing passes", "several
  candidates", "other map, confirming", "nothing found yet".
- `stale`: no fresh scan (the bridge's existing `scanning()` is false),
  whatever the pose. The other four states exist only while scanning. The
  pose, if any, is kept and shown softly; the first scan after the wake
  word or boot enters `checking` (pose) or `searching` (none).
- `off_map`: 20 s of searches with no candidate passing on any map, counted
  only over scans with two thirds of their beams counted (a scan that could
  not commit a candidate cannot argue for no map either); the clock does not
  advance across other scans. 20 s so it lands inside a held trip's
  deadline. The search keeps running; a clear winner leaves for `checking`.

**Dropping a pose.** A pose fails a verdict scan when it does not pass it or
when a rival stands against it. After N consecutive failing scans the pose
is dropped: `searching`, and a running trip is stopped and held. That is
the whole rule. A wrong room, a wrong heading, a drag, a carry with the
lidar on, the counter, an unmapped room, a hallway slip, a wrong seed all
fail this way; a moved chair, an open door, a removed table, a person never
do, because the patch rule and the short class absorb them. There is no
special case for a map change and no state that survives one.

Why this is enough. A contradiction fails N scans in a row at once and goes
straight to `searching`; a wrong seed fails the same way; a pose that
agrees in the wrong place is out-scored by the true pose within one search
and N scans more. A plain hallway driven from a known pose is neither: the
side and end walls agree, nothing goes through, no other pose beats it by
R, and the robot stays `tracking` on odometry. Floor run 1's lesson, never
re-seed a tracking AMCL mid-drive, is built in: the search commits only
over a pose that has failed N scans. A carry with the lidar on is expected
to fail during the carry itself (turning scans are skipped, the rest see
over the furniture to walls behind, and the search finds no committable
pose from mid-air); phase 1 records one in a furnished room and a bare one
to check, and if either slips past the judge the anchored scan check comes
off the shelf.

What each state allows:

| state | trips | gamepad | emotes, in-place | barriers | save place |
| --- | --- | --- | --- | --- | --- |
| tracking | yes | yes | yes | from the pose, plus sensors | yes |
| checking | start or release when clean or uninformed for 5 s, else held; a running trip continues | yes | yes | from the pose, plus sensors | no |
| stale | held | yes | yes | from the last pose, plus sensors | no |
| searching | held; a running trip is stopped and held | yes | yes | from TF's pose, plus sensors | no |
| off_map | refused, with the one sentence | yes | yes | sensors only | no |

Emotes hold the nav lock for two seconds, as today, and the nav lock ends a
held trip exactly as it ends a running one. Save place is refused in every
state but `tracking` with one text beside the button, "drive me a little
first" (right even in `stale`: a gamepad drive wakes the rail and the lidar
with it), and the voice's `create_place` tool turns the bridge's
`not_localized` error into that same sentence as its note. Barriers come
from whatever pose TF has in every state but `off_map`: a soft or doubted
pose is still the best guess, the barriers mark the steps (the floor test
plan's check 30 fix, kept), and the docstring's rule stands, a wrong stop
over no stop. `off_map` is the one exception because a robot in a
genuinely unmapped room would otherwise stop at phantom steps for as long
as it stayed; the cost is that a false `off_map` loses map barriers until
the next find, with the cliff and forward sensors still guarding. One
accepted wrinkle, no worse than today (where `localized` stays true through
a carry): a robot carried while asleep publishes barriers from its old pose
until the first search commits, so a robot that was parked by the steps may
stop once on its first gamepad push in the new room; it clears itself
within seconds.

**Numbers.** The bars read off recordings in phases 1 and 2 and stored in
`robot_nav.py` beside the pure functions, each with its recording named in
the comment as `SCAN_FIT_*` is today: A, T, M, R, the agree tolerance, N.
The design constants, chosen and checked against recordings but not derived
from them: the third of beams for a verdict and the two thirds for a
commit; the 2× tolerance factor for through and its 6 m cap; the ±3 degree
neighbour rule; the 0.3 m motion gate; the 5 s uninformed release; the
0.3 m / 10 degree rival distance; the 20 s `off_map` clock; the 4 m beam
cap and 20 cm / 3 degree steps of the coarse search; the sixty candidates;
the two ticks before a seed is re-sent; the two clear wins for a map
switch; the 30 s trip deadline and the 3 s before "hang on" (part 3).

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
  is 1000 poses, 1300 with the dilation, times 120 headings, times 60
  beams: 9 million gathers. No ray casting here: ray casting every grid
  pose is billions of samples.
- The pooled score saturates: in a house a large share of coarse cells put
  every end point within the window of some wall and score full marks, and
  from a hallway dozens of positions tie with the true one. So ties at the
  top are broken by the sharp score at the same pose (one more gather per
  tied pose, a few million at most), and the **sixty** best distinct peaks
  are kept by non-maximum suppression in that order: a kept peak suppresses
  every cell within one grid step and one heading step of it, and
  suppression does not chain. This departs from Olson's rule of expanding
  every coarse cell whose bound beats the best fine score so far: that rule
  guarantees the fine maximum, and the fine maximum, "end points near
  walls", is exactly the score the floor has shown we cannot trust; the
  list is wide instead, and the judge decides.
- Refine each of the sixty on the sharp grid over ±35 cm and ±5 degrees at
  5 cm and 1 degree on every third beam (120): about 2500 poses times 120
  beams times sixty, 18 million gathers. Per axis, one grid step plus the
  half-step offset is 30 cm inside the 35, and one heading step plus its
  half is 4.5 degrees inside the 5, so the true pose is inside the window
  of the peak that absorbed its cell. Pooling leaves each real mode as a
  plateau up to three cells wide, so one mode can take several slots; after
  refining, results within one grid step and heading step of a better one
  are merged. Then run the ray-cast judge on the survivors, all 360 beams,
  about 3 ms each on the Pi. The whole pipeline, about 30 million gathers
  and their numpy passes plus sixty judges, is timed on the robot in phase
  2 against the two-seconds-per-map bar; if it misses, the knobs in order:
  every twelfth beam in the coarse pass, a 3 m cap, forty candidates, the
  FFT pass on the shelf. The coarse and sharp scores only choose which
  poses get judged; the judge decides, by agree and through shares, never by
  raw score, and the same shares rank candidates across maps.
- **Rules, written as formulas.** A candidate **passes** when agree ≥ A + M
  and through ≤ T and at least two thirds of its beams are counted (a pose
  on the map's edge with most of its beams in unknown is not a find; the
  judge's one-third rule is for a pose already held). The commit bar A + M
  is the hysteresis: the judge drops at A, so a candidate that only just
  passed the judge would be dropped by its first wobble and found again a
  second later, for ever. Phase 1 reads A so that A + M still sits below
  every unambiguous true pose recorded, the true pose beside the removed
  table included (its through beams are the dropped patch, so they are not
  in its agree denominator); if one cannot be committed, A was set too
  thin, not M too wide. M comes from the wrong-room recording. The
  **winner** is the passing candidate with the highest agree, and it is
  **clear** when every other passing candidate is at least M behind it in
  agree or has more through. There is no prior: the last known pose is a
  candidate like any other (it is TF's pose, scored on every search), and
  a tie between two passing candidates is broken by the next scan from a
  new spot, not by where the robot used to be.
- **No carried state, no lift sensor, no detector.** Arlen: "Do I know
  where I am? Is there another point that makes more sense? covers drag,
  carry, everything." It is one search, run the same way in every state;
  only the reading of its result differs: a rival in `tracking` and
  `checking`, a commit in `searching` and `off_map`. A seed from a mid-air
  scan enters `checking` and must pass a verdict scan before any trip is
  released on it; the wheels do not turn in the air so it cannot earn
  `tracking`, and the next search overwrites it when it fails.
- **Believe, then confirm by motion.** A clear winner is committed at once:
  seed AMCL (transported, part 1), enter `checking`; `tracking` follows N
  passes and the 0.3 m gate. If no winner is clear the state stays
  `searching` ("several candidates"), the trip stays held, and the next
  search uses the next scan; as the robot moves, one fresh scan from a new
  spot breaks the tie. No candidate tracking across scans, no nudge: the
  robot never moves itself to localize. A tie nothing resolves runs out
  the held trip's 30 s and the give-up is spoken; the caption already says
  moving the robot helps.
- **Which map.** The search runs on every saved map, read from the PGM
  files on disk (`/map` carries only the loaded one), and the winner names
  the map. A clear winner on another map is not committed on its own; the
  state stays `searching` with the reason "other map, confirming", and the
  second consecutive clear win on that map switches (a mid-air scan during
  a carry must not reload the map and costmaps every second). The bridge
  asks map_server to load the other file with the asynchronous service
  call, so the tick never blocks; the judge is muted from the request until
  the new `/map` has arrived at the bridge and the seed has landed by the
  arrival rule of part 1 (a map load leaves AMCL's particles where they
  were, which on the new map is simply a wrong pose; verified in Nav2's
  source, checked once on the robot in phase 2). A held trip whose place is
  on the old map ends `failed` with the existing `place_on_other_map`. A
  learner reload of the same map needs no re-seed: the origin never moves,
  so the particles are still right; only the mute applies.
- **Not on a map.** 20 s of searches with nothing passing on any map:
  `off_map`, as above.

Where it lives: one `threading.Thread` per search, started from the tick
whenever the lidar is scanning and no search is running, on the latest
scan with a verdict (never a no-verdict scan, never one older than TF's
ten-second cache); one result attribute that the next tick reads. Its own
flag, not the bridge's `busy` activity lock that relocalize uses today: an
activity refuses trips, pauses the state machine and resets the pending
goal's clock every tick, all of which the trip rules forbid. No queue, no
result class, no localizer object; the maths is pure numpy functions in
`robot_nav.py` (or a sibling module if it outgrows the file) with tests on
synthetic and recorded scans. The search takes up to two seconds per map
and runs continuously while the lidar is on, which is minutes at a time;
numpy releases the GIL for its big gathers, but whether the tick and the
barrier publisher stall is measured in phase 2; if they do, the search
moves to a subprocess.

**Held trips** reuse the pending slot the bridge already has
(`goal_request`, today a tuple, with `GOAL_START_SECONDS` = 30 s): the
pending request becomes a dict with a `state` of `held` and a
`held_seconds`, and a dropped pose writes the running goal back into that
slot instead of cancelling it. Waiting for the lidar and Nav2 to come up
keeps today's clock and today's reasons, unspoken. `held_seconds`
accumulates on every tick the trip is held for the localizer (the state is
`searching`, or `checking` that is neither clean nor uninformed for 5 s)
and never resets, so one 30 s deadline covers all of a trip's localizer
holds together. Two consequences, intended: a trip asked for from a cold
idle on the counter can take up to a minute to give up (today's bring-up
clock, then this one); a long trip held 5 s at its start and dragged a
minute later has 25 s left to re-find and resume. The trip keeps its one
serial, the one in the reply, for its whole life; a release is a new Nav2
goal under the same serial, and the bridge tells a late result from the
stopped Nav2 goal apart by its goal handle, so it cannot kill the resumed
trip. The bridge's `navigating()` counts `held` as in flight, so the rail
and lidar stay up through a hold and every path that gates on it sees the
held goal: "stop", the dashboard's Cancel trip button, a gamepad twist and
the nav lock cancel a held trip (the person took over; they can ask
again), the voice's `check_navigation` keeps waiting through it, and the
map draws a held goal hollow. New behaviour, with a test: a request for
the same place during a hold returns the held goal, accepted, under its
serial (today every request replaces the goal and bumps the serial, which
would reset the voice's line memory and repeat "hang on"); a different
place replaces it as today. When the deadline passes the goal ends `failed`
with reason `lost`; when the state reaches `off_map` a held goal ends at
once with reason `off_map`. The deadline is what bounds the robot stuck on
a rug: wheels turn, odometry advances, the world does not, which reads as
a dropped pose, a re-find at the true pose and a resume into the same rug;
without the deadline that loop is silent and endless.

**The voice** has no standing poll today: the trip tool returns the moment
the bridge accepts, and the goal is looked at again only if the model
chooses to call the check tool. So a held trip is the tool's to wait out,
but only when the robot already has something to say about it: when the
bridge's reply carries `accepted: true`, the serial, `held: true` and a
state of `searching` or unclean `checking`, the trip tool keeps polling
`nav_status` inside the call until the trip leaves, is cancelled or fails
(bounded by the deadline), and only then returns to the model. A hold for
the lidar or Nav2 coming up (`stale`) returns at once as today, so a trip
asked for from idle is acknowledged at once and leaves on its own, and
whatever happens after the tool has returned is dashboard-only for now: a
drag after the trip left, a carry while asleep whose search fails, the rug.
Giving the robot a voice during a trip it is no longer being asked about is
the UX audit's cluster A, not this plan. The canned lines are spoken from
the poll as the turn's own speech: the tool context on both of the voice's
paths (the plain assistant turn, which is where "go to the kitchen" runs,
and the goal runner) gains the speaker, the poll waits for the turn's own
narration to finish before a line, and a line is the turn's playback, so a
"stop" shouted over it is a barge-in. Today a barge-in on an explicit stop
word cancels the turn and returns to listening without running any tool,
and during a working turn a stop word can be stitched into the next
request; so the trip tool's poll, when its task is cancelled, sends the
bridge a cancel for its serial before it dies, and the stitching branch
routes an explicit stop word to the stop tool. Which lines have been said
is remembered per goal serial in one place in the voice module, so a second
ask about the same trip (which returns the same serial) never repeats one.
What the model sees is a normal trip in flight, or a handled outcome with
the reason word as today and, when a line was said, `spoken: true` with the
note "the robot already explained this out loud; a short acknowledgement at
most"; the tool dispatcher treats a result carrying `spoken: true` as
handled, not an error, whatever its `accepted` flag says. Nothing was said,
nothing is claimed: the model tells the user what happened in a sentence,
as it does today. A refused trip in `off_map` carries the same `spoken`
note. `where_am_i` keeps its boolean and a canned note, "the robot sorts
this out itself, just ask for the trip", or in `off_map` the off-map
sentence, never the state word. The trip tool's description loses "spins
in place to find itself", says the call may take up to half a minute to
leave, and that the tool speaks for itself about position. The three
lines, spoken verbatim and only in these cases:

- "Hang on, I'm working out where I am." Once per trip, when the trip has
  been held with the state `searching` for about 3 s. If this line is ever
  heard after an ordinary idle, that is the pre-trip pause leaking through
  the speaker, and the fix is the lidar rules, not a nicer sentence.
- "I've lost track of where I am. Move me a little and ask again." Once per
  trip, when the poll sees the goal `failed` with `lost`.
- "I can't work out where I am from here. Put me on the floor somewhere I
  know." When a trip is asked for in `off_map` (from the reply), or the
  poll sees its goal end `failed` with `off_map`; never unprompted.

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
- **Votes**, only while `tracking`, within 4 m (where 1 degree beams are
  still closer together than two cells), hitting the wall at more than 20
  degrees from grazing. A through beam, including one in the dropped patch,
  votes free on the wall cell it crossed; a short beam votes occupied where
  it stopped; an agreeing beam votes *for* the wall cell it hit, which can
  only block or reset a change, never make one. That is what stops the
  learner eating itself: a pose 10 cm off for a whole lap casts only
  confirming votes, because 10 cm is inside the agree tolerance.
- **Visits and verdicts.** Votes on a cell until it has had none for 30
  minutes are one visit; a visit's verdict is clear at 20 votes, 90 percent
  one way; a cell changes after three visits against it with none for it in
  between (defaults accepted by Arlen). Both gates are at least as strict
  as the clamped log-odds update lifelong mappers use.
- **Reload.** The bridge asks map_server to reload by the `load_map` service
  it already uses, asynchronously, judge muted until the new map arrives,
  then rebuilds the score grids. No re-seed: same map, same origin.

### What this gives Arlen's four points

No spin: one scan, whole map, judged candidates in a second or two,
confirmed by the first 0.3 m of the trip or whatever drive is already
happening. Continuous: the judge runs every tick and the search runs the
whole time the lidar is on; a pose that contradicts the map or is beaten by
a better one is dropped without a button. Which map: the search scores all
maps and picks the one that survives. Accurate: nothing is `tracking`
without a high agree share, a low through share, no better pose anywhere
on any map, and motion the judge agreed through; AMCL is configured so it
can actually hold 10 cm. A 55 percent match of any kind is `searching`,
and the marker says so.

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
- **Shares alone, no rival.** A slip along a corridor or a half turn in a
  room is invisible to any single-scan contradiction score; a simulated
  house gave a 180 degree flip agree 0.73 and through 0.05 after the patch
  rule. Asking "is there a better pose anywhere" is the only honest answer.
- **A rival check as a separate windowed search** (1.5 m / 30 degrees
  around the pose, once a second). Its window could not hold the metre-off
  case it was calibrated on without costing more than the whole-map search,
  and the whole-map search was idle while `tracking` anyway. One search,
  read two ways.
- **A prior that favours candidates near the last known pose.** Wrong after
  a map switch, wrong after a drag in a near-symmetric room (the stale
  mirror pose is inside the prior and wins), and the plan already breaks
  ties with the next scan from a new spot.
- **Forgiving a `tracking` pose that fails on through alone** (a special
  case for map changes, with its own search and learner clause). It could
  not help a `checking` pose that woke facing a removed table, which then
  declared itself off its own map, and it could keep a wrong pose over a
  passing true one. The patch rule in the judge handles the map change in
  every state with no state at all, and is the rule Arlen asked for.
- **Judging "seed landed" by stamps.** AMCL's one forced `/amcl_pose` after
  a seed carries the scan's stamp, earlier than the seed's; a stamp rule
  would re-seed for ever.
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

- **Patch width cap**: the dropped patch may be no wider than some angle; if
  a wrong-pose recording passes only because its through sat in one patch.
- **Short-blind agree**: agree over counted beams minus short ones when
  through ≤ T; if the standing-guest recording drops agree under A.
- **Coarse through term**: two samples along each beam in a dilated wall
  grid, subtracted; if a labelled true pose misses the sixty candidates.
- **FFT correlation pass** for the coarse search (about 0.75 s at 512 cells,
  extrapolated from a 0.17 s test at 256); if the plain grid misses the
  two-second bar after its own knobs.
- **AMCL global init with 50 000 particles**; if the numpy search cannot be
  made to fit the Pi.
- **Anchored scan check** (first scan of a still period kept and compared
  against each new one); if a phase 1 carry recording slips past the judge.
- **Refine-step tracker** (the sharp-grid refine run every scan around the
  odometry pose, with a penalty toward it, replacing AMCL); if AMCL misses
  10 cm or 5 degrees at the crosses in phase 1.
- **Scan de-skew by odometry yaw rate**; if the ±3 degree neighbour rule
  is not enough for scans taken while Nav2 turns under the gate.
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
  saves the latest scan (with its stamp, receipt time, `scan_time` and point
  count), the lidar's TF map pose, AMCL's pose and odometry to a JSON lines
  file under `~/.config/robot-pet/recordings/`; with `seconds` it appends
  every tick's scan for that long. A button in the debug panel sends it.
  The first recording settles the revolution time and the stamp convention.
  Truth comes from three or four taped crosses, each tape-measured from two
  mapped walls with a taped heading line; a recording at a cross carries
  that pose as its label, elsewhere Arlen's sentence is the label.
  Tonight's wrong room and the metre-off pose are the first two entries.
  Further recordings: true pose at the crosses and the places, right spot
  wrong heading, on the counter, on the rug (tilt lets beams see over low
  furniture), a carry with the lidar on through a furnished room and a bare
  one, a drag along the hallway, a drag two metres sideways across the
  living room, a gamepad drive around the floor while correctly localized
  (as a sequence, for the motion gate, AMCL's drift and R's noise floor), a
  drive past the open door and the dark sofa, a true pose beside the
  removed coffee table, a person walking past, and a person standing a
  metre in front for 30 s at the weakest-scoring cross.
- `tools/localization_replay.py` prints today's `scan_fit` and the new
  `agree` and `through` shares (with and without the patch rule, at ±2 and
  ±3 degrees) for every recording, AMCL's error at each cross, and which
  angular sectors are always short (the robot's own body, masked once).
  That table goes into this document and A, T, the tolerance and N are read
  off it, with margin. Pass for the judge: every true pose (the open-door,
  rug, removed-table, walking-past and standing-guest recordings count as
  true) passes, and the wrong room and the counter fail with the patch rule
  applied, with daylight between. The wrong heading, the sideways drag, the
  hallway drag and the metre-off pose are reported, not required: a pose
  that agrees with the map in the wrong place is the search's case in phase
  2, and if the wrong heading passes on shares that is written here, not
  hidden. If a wrong pose passes only through the patch rule, the width cap
  comes off the shelf; if the standing guest fails, short-blind agree; if
  nothing separates the wrong room and the counter, this plan stops and is
  rethought rather than tuned. The carry recordings decide the anchored
  check; the cross recordings decide the tracker.
- Map audit: the replay draws where through beams land at the known-true
  poses. Clusters on a few cells are map errors (diagonal walls with corner
  gaps included): fix those cells or re-map (doors in their usual state,
  nobody walking, slow drive); if unknown speckle inside rooms swallows
  beams, fill unknown islands that do not touch the border at map load.
  Spread everywhere means the tolerance is wrong, not the map. The verdict
  on `groundfloor` goes into this document before any bar is set.
- `robot_nav.beam_check(...)`: the ray cast at half-cell steps, the
  seven-beam neighbour rule, the patch rule over counted beams, no-return
  and unknown as no opinion, the no-verdict rule; pure numpy, tests on
  synthetic maps and on the recordings, which run in the normal suite. The
  judged pose is the single-edge latest `map -> odom` composed with
  `odom -> laser` at the scan's middle; the turn gate reads odometry yaw.
- The state machine in the bridge with four states, `tracking`, `checking`,
  `searching`, `stale`, one count N, the motion gate, clean after one pass,
  the uninformed release after 5 s, the tick judging before it releases,
  dropping after N failures, the seed-landed arrival rule. `localized`
  becomes "tracking or clean checking". Entering `searching` cancels a
  running trip through the cancel path that already exists. Save place
  needs `tracking`, with its refusal text beside the button and in the
  voice's refusal note. The pre-trip spin and the boot spin stay in this
  phase: `try_map` accepts a spin when `beam_check` passes the scan at
  AMCL's pose *and* today's `scan_fit` bar holds (the second condition is
  kept one phase longer because today's score does tell the wrong heading
  from a true pose, 0.44 to 0.49 against 0.55 and up, and the judge may
  not); a trip whose spins all fail ends `failed` with reason `lost`
  (today's `not_localized`, renamed once; the model paraphrases it as it
  does today). `scan_fit`'s `localized` bars go now; the score itself, the
  turn-gate constant and the ring dilation go in phase 2 with the spins.
  "Worse" in this phase means a wrong room accepted, not fewer trips
  started.
- AMCL: `update_min_d` and `update_min_a` 0.1, `sigma_hit` 0.1, seed
  covariance 0.02 and 0.01, seed transported by the bridge and stamped with
  the newest odometry time.
- Voice: the trip tool's description loses "spins in place to find itself".
- Dashboard: the marker (solid, soft, hollow, none) and the caption in
  front, with the `searching` caption "lost, press Localize" until phase 3
  ("finding my place" while a trip's spin is running); state word, shares,
  reason text, map name behind the debug toggle.
- Exit, on the floor: the wrong room is never shown as `tracking`, and a
  spin from the wrong room is never accepted; a correct pose reaches
  `tracking` within N scans after a seed and a short drive; the counter
  drops to `searching` within N scans; a robot parked nose to a wall still
  leaves on a trip on its first verdict scan; a robot facing the open front
  door leaves within 5 s; a robot carried while asleep shows `stale`, never
  a solid marker in the old room, and never drives off on the old pose;
  Save place is refused with its text until `tracking`; a correctly
  localized robot driven by gamepad through the hallway and into corners,
  past a moved chair, an open door, over the rug, past the dark sofa, past
  the removed coffee table and past a person standing in front, stays
  `tracking` the whole way; a robot put to sleep beside the removed table
  and woken there is clean `checking` on its first verdict scan; AMCL's
  error at the crosses is written down; and the replay's verdict on the
  wrong heading and the drags is written down for phase 2.

### Phase 2: the search (four evenings)

- `robot_nav.global_search(...)`: the score grids for every PGM, the coarse
  pass at 20 cm and 3 degrees on 60 beams capped at 4 m, sharp tiebreak,
  non-maximum suppression, sixty candidates, sharp refinement over ±35 cm
  and ±5 degrees on 120 beams, merge after refine, judge on the survivors,
  the pass (A + M, two thirds counted) and clear (M lead or more through)
  formulas, the current TF pose scored as a candidate when there is one.
  The whole pipeline timed on the robot against the two-seconds-per-map
  bar, and the tick and barrier publisher watched for stalls while it runs.
  Tests on recordings: every labelled true pose at an unambiguous spot is
  among the sixty and is the clear winner after the judge; the wrong room
  recording produces no passing candidate at the wrong room; the counter
  recording produces none; a map-edge pose with most beams in unknown is
  not committed; the wrong heading, the hallway drag and the metre-off
  recordings each produce the true pose as a rival over the recorded pose,
  and that sets R's ceiling; the gamepad-drive sequence never produces one,
  and sets R's floor; M comes off the wrong-room recording.
- The search thread runs whenever the lidar scans, one search after
  another; the result is read as a rival in `tracking` and `checking` (a
  standing rival fails every scan until the next result) and as a commit in
  `searching`. Relocalize becomes "search now": the Localize button drops
  the pose to `searching` (cancelling a running trip, as in phase 1); the
  pre-trip spin, the boot spin, `RELOCALIZE_SPINS`, `scan_fit` and its bar
  are deleted. A trip pending in `searching` is released when a winner is
  committed and goes clean, or ends `lost` at 30 s, so a voice trip from a
  lost robot works in this phase and a tie is broken by whatever motion
  happens. The button stays in front only until phase 3 gives the lost
  robot its voice.
- All saved maps are searched; the winner names the map after two
  consecutive clear wins; the map load is asynchronous and the judge is
  muted until the new map arrives and the seed has landed; a held trip to a
  place on the old map ends `place_on_other_map`. Checked once on the robot:
  a `load_map` leaves AMCL's pose where it was.
- AMCL `recovery_alpha_slow` and `recovery_alpha_fast` to zero; the search
  owns recovery now.
- Exit, on the floor: carried to twenty spots, set down, Localize pressed:
  the right candidate within 5 s where the spot is unambiguous, `tracking`
  within the first 0.3 m of a trip, never a wrong room, zero misses (one
  miss is a bug to find in the recordings, not a statistic); at the crosses,
  within 10 cm and 5 degrees; a search on the counter stays `searching`,
  never committed; a robot slipped a metre along the hallway, and a robot
  turned half round by hand in the living room, each drop to `searching`
  within a few seconds and re-find the true pose; a tie (the two matching
  corners of the living room, if there are any) breaks within a metre of a
  hand nudge while a trip is pending, or of gamepad driving after which the
  trip is asked for again; the robot is driven round the floor by gamepad
  for ten minutes and is never dropped; with a second saved map (map one
  other room for the test), carry the robot onto it: the map switches after
  two clear wins and a trip to a place on it runs.

### Phase 3: held trips and the voice (three evenings, one exit blocked)

- The pending goal becomes a dict with `held` and `held_seconds`; a dropped
  pose moves the running goal into it instead of cancelling; a release is a
  new Nav2 goal under the same serial; `navigating()` counts `held`; the
  same-place request returns the held goal; the trip leaves on its own from
  `tracking`, a clean `checking`, or a `checking` uninformed for 5 s. The
  reply carries the serial, `held` and the state, or the refusal reason;
  the give-up ends the goal `failed` with `lost`, `off_map` ends it with
  `off_map`. The voice's trip tool waits out a localizer hold inside the
  call, returns at once for a `stale` hold, speaks the three canned lines
  as the turn's own speech on both voice paths, cancels its serial when its
  task is cancelled, returns the reason word and, when a line was said,
  `spoken: true` and the note; remembers lines per serial; the stitching
  branch routes an explicit stop word to the stop tool; `where_am_i` gains
  the canned notes; the tool description says the call may take half a
  minute.
- `off_map` after 20 s of two-thirds-counted searches with nothing passing;
  a held goal ends `off_map` on that edge; barriers from sensors only in
  `off_map`. The Localize button moves behind the debug toggle, with the
  candidate list.
- Dashboard: the goal row shows "held", and "cancelled (gamepad)" when a
  push ended one; a held goal is drawn hollow on the map; a refused trip
  from a map click shows in the goal row, not the log; the `searching`
  caption becomes "finding my place, moving me by hand helps"; the
  `off_map` caption points at the existing Start mapping button, no new
  button.
- The lidar starts on the wake word. This needs the lidar power plan, which
  does not exist yet (today the lidar follows the rail, which powers the
  motors); until it exists the first trip after an idle still waits for the
  lidar and says nothing, because that hold is `stale`, not `searching`.
  Written down so it is not read as a bug later: once it exists, every wake
  word spins the lidar audibly and brings Nav2 up for about a minute.
- Exit, on the floor: say "go to the kitchen" from idle: the robot
  acknowledges, leaves with no wiggle and says nothing more (no pause at all
  needs the lidar power plan). Drag the robot mid-trip two metres and turn
  it: the trip stops within N scans, the state goes `searching`, it
  re-finds itself and resumes, and the marker and caption tell the story
  without a button press. Carry it to another room: same, minus the resume.
  Drag it while awake, then ask for a trip: "hang on" at 3 s, then it
  leaves. Say "stop" during that hold, and once over the "hang on" line:
  the trip is gone and nothing resumes. Ask twice during a hold: one "hang
  on", nothing else. Put it on the counter and ask for a trip: `searching`,
  "hang on" at 3 s, `off_map` at 20 s with the off-map sentence once, no
  trip, no lost line. Lidar off, carry it, wake word: `stale`, `checking`,
  a search, never the old pose shown as tracking. Park it on the rug so it
  slips: stops and resumes end inside the deadline with the dashboard
  saying `failed (lost)`, the voice silent (dashboard-only until cluster
  A). Remove the coffee table, awake and across a sleep: the robot stays
  `tracking`, trips run. A drive past the dark sofa and through the hallway
  at speed never stops a trip. A gamepad push during a hold ends the trip
  and the goal row says so. Nothing about position is ever said twice
  unprompted.

### Phase 4: the learner (re-planned at phase 3 exit; about three evenings)

- `robot_nav.map_votes(...)` (votes from one scan's beams at a tracking
  pose, reusing the judge's ray cast) and `robot_nav.apply_visits(...)`
  (visits, verdicts, the three-visit rule). Tests on synthetic maps: a
  removed box fades after three visits, not two; a wall under a constant 10
  cm pose offset never moves; a door seen open twice and closed once stays;
  nothing changes from a `checking` or `searching` pose.
- The learner runs in the bridge, writes the PGM and the evidence file
  atomically when idle, reloads with the judge muted, rebuilds the score
  grids. The pristine copy is made on first run if missing.
- Map card: "forget what you learned", with a count, shown only when there
  is something to forget; the change overlay is in the debug panel only.
- Exit, on the floor: remove the coffee table; after three visits on
  separate occasions it is gone from the map and the judge's dropped patch
  there is gone with it. Localization accuracy is unchanged across the
  phase; the map's origin, places and barriers are untouched.

Total: twelve evenings for localization that just works, plus about three
for the learner.

## Dashboard words

One marker and one caption per state, in front of the debug toggle:

| state | marker | caption |
| --- | --- | --- |
| tracking | solid | none |
| checking | soft | "checking my position, sure once I move" |
| searching | hollow, at TF's pose | "finding my place, moving me by hand helps" (phases 1 and 2: "lost, press Localize", or "finding my place" while a trip's spin runs) |
| stale | soft, at the last pose, or none | "lidar off" |
| off_map | none | "can't find my place here. Put me somewhere I know, or start mapping below." |

Save place refusal beside the button: "drive me a little first". Goal row:
"held", and "cancelled (gamepad)" when a push ended a hold; a held goal is
drawn hollow on the map. Behind the toggle: state word, agree and through
shares, the reason text, the candidate list, the Localize button, and the
learner's change overlay. A robot that wakes and is never driven stays
`checking` honestly; the motion gate is what "confident" means.

## Decisions

The settled list, pointers only; the numbers and the reasons live in The
design, once each. The dated history of how each was reached is in git
(`git log -- "docs/plans/2026-10-26 - localization-plan.md"`).

Arlen's, by his word:

- No carry or drag detection of any kind; "do I know where I am, is there
  another point that makes more sense" covers everything.
- Everyday changes never cost `tracking`; a door's through beams in one
  patch are "something changed", not a contradiction, as long as the rule is
  a common pattern and not janky. (That rule is the patch rule, part 2.)
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

Claude's, as final arbiter after nine review rounds:

- AMCL stays the tracker; the refine-step tracker waits on its measured
  error at the crosses. The bridge transports the seed and the arrival of
  `/amcl_pose` says when it landed.
- One drop rule, no exceptions: N failing scans, where a standing rival
  fails a scan. Map changes are the patch rule's job, not a state's.
- One search, whole map, every saved map, running the whole time the lidar
  is on; its result is a rival in `tracking` and `checking` and a commit in
  `searching`. No windowed search, no prior.
- The search commits at A + M, the judge drops at A: hysteresis, one number
  reused. Sixty candidates, ties broken by the sharp score, judged by
  shares; Olson's bound expansion is deliberately not used.
- Held trips reuse `goal_request`; one serial per trip; `held_seconds`
  accumulates only while the hold is the localizer's.
- The trip tool waits inside the call only for a localizer hold and speaks
  the canned lines as the turn's own speech, on both voice paths; the model
  always gets the reason word and never gets told a line was said when it
  was not; after the tool has returned the dashboard is the only voice
  until cluster A.
- Barriers from TF's pose in every state but `off_map`.
- Phase 1 keeps the spins, judged by `beam_check` and today's bar together;
  phase 2 deletes them with the search and zeroes AMCL's recovery; `off_map`
  and holds arrive in phase 3.

## Sources

- [Olson 2009, Real-time correlative scan matching](https://april.eecs.umich.edu/pdfs/olson2009icra.pdf), [Olson 2015, M3RSM](https://april.eecs.umich.edu/pdfs/olson2015scanmatch.pdf)
- [Hess et al. 2016, Real-Time Loop Closure in 2D LIDAR SLAM](https://research.google/pubs/pub45466); [Cartographer pose_graph.lua](https://github.com/cartographer-project/cartographer/blob/master/configuration_files/pose_graph.lua) for the 0.55 / 0.6 bars
- [prism_loc, 2D MCL with branch-and-bound global relocalization and verification](https://github.com/kjungmo/prism_loc), [parameters](https://github.com/kjungmo/prism_loc/blob/main/PARAMS.md)
- [Nav2 AMCL configuration](https://docs.nav2.org/jazzy/configuration_and_development/configuration_guide/others/configuring_amcl/); Nav2 `nav2_amcl/src/amcl_node.cpp` (`handleInitialPose`, `initialPoseReceived` map range check, `laserReceived` forced publication after a seed, `publishAmclPose` stamped with the scan, transform post-dating, `handleMapMessage`), `pf/pf.c` (recovery weights) and `sensors/laser/likelihood_field_model.cpp` (cubed beam weights summed); `nav2_map_server/src/map_server/map_server.cpp` (`loadMapCallback` publishes latched)
- [Beluga AMCL](https://github.com/Ekumen-OS/beluga)
- [slam_toolbox README](https://github.com/SteveMacenski/slam_toolbox), [JOSS paper](https://joss.theoj.org/papers/10.21105/joss.02783.pdf)
- [Cartographer tuning](https://google-cartographer-ros.readthedocs.io/en/latest/tuning.html)
- [Thrun et al., Robust Monte Carlo localization (Mixture MCL)](https://www2.cs.sfu.ca/~vaughan/teaching/415/papers/thrun_robust.pdf); Thrun, Burgard, Fox, Probabilistic Robotics, ch. 6.3 to 6.4 (beam model and its brittleness) and ch. 8.3.5 (Augmented MCL)
- Lenser and Veloso 2000, Sensor Resetting Localization; summarised in [Sehestedt and Schneider, MCL in highly symmetric environments](https://www.scitepress.org/Papers/2006/12158/12158.pdf)
- [Dreame multi-floor guide](https://www.dreametech.com/blogs/blog/multi-floor-mapping-robot-vacuum), [Valetudo multi-floor thread](https://community.home-assistant.io/t/multi-floor-vacuuming-using-valetudo-and-dreame-robots/933706)
- [Walcott-Bryant et al., Dynamic Pose Graph SLAM](https://people.csail.mit.edu/kaess/pub/WalcottBryant12iros.pdf); Hornung et al. 2013, OctoMap, for clamped log-odds updates
