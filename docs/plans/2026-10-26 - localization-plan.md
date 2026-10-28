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
it is handed a true pose, configured for a 5 cm map, and told when to stop
trusting itself.

The fix is the structure every working system in the literature has, and
that we are missing: a **tracker** (AMCL, kept) plus a **judge** (a strict
score that counts beams passing *through* mapped walls, and a confirmation
step over a little motion before anything is believed) plus a **search**
(match one 360 degree scan against the whole map, every saved map, by
brute-force correlation, no spinning) plus, once those three hold on the
floor, a **learner** that keeps the one map true as the house changes. The
judge runs all the time; the search runs whenever the judge is unhappy. That
one question, "do I know where I am, and is there another spot that makes
more sense", covers drags, carries and reboots alike. No lift sensors, no
carried state, no detection of anything. From Arlen's chair the whole thing
is invisible: the lidar wakes with the wake word, a trip leaves without a
pause or a wiggle, and the robot speaks only when it has genuinely been
moved somewhere it cannot place. One thread in the nav bridge, one module of
pure functions, five states, three phases plus the learner.

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
- AMCL is configured for a coarser map than ours: `sigma_hit` 0.2 m makes
  its likelihood flat within 20 cm, so it cannot resolve a 10 cm target no
  matter how many beams it uses; the seed covariance (32 cm, 15 degrees) is
  three times wider than the error of the pose being handed in.

## How people actually do this

Short version: every system that relocalizes reliably has the same three
parts. A tracker that follows the robot when its pose is roughly right. A
global search that finds candidates from a single scan without moving. A
confirmation step that refuses to believe a candidate until it has survived
a little motion or clearly beats every alternative.

**Trackers.** Nav2's AMCL is still the default localizer for a prior map
([Nav2 AMCL docs](https://docs.nav2.org/jazzy/configuration_and_development/configuration_guide/others/configuring_amcl/);
a [2026 vehicle paper](https://arxiv.org/pdf/2604.24242) kept it). Beluga
AMCL is a modern drop-in reimplementation with the same parameters, demoed on
a Raspberry Pi 4B ([Beluga](https://github.com/Ekumen-OS/beluga),
[ROSCon 2024 talk](https://roscon.ros.org/2024/talks/Beluga_AMCL-_A_modern_Monte_Carlo_Localization_implementation_for_ROS.pdf));
there is no Lyrical apt package for it. slam_toolbox's localization mode
keeps a rolling buffer of scans in a pose graph on top of a serialized map;
it is reported more precise than AMCL but needs a serialized pose graph (not
our PGM), "quite a bit of tuning", and has no global search
([slam_toolbox README](https://github.com/SteveMacenski/slam_toolbox),
[ROS Answers](https://answers.ros.org/question/365472/)). Cartographer's
pure localization does carry a global search, but Lyrical ships only its
message package and the project is in maintenance
([Cartographer tuning](https://google-cartographer-ros.readthedocs.io/en/latest/tuning.html)).
Scan-to-map correlation every tick (Cartographer's real-time matcher, Karto's
scan matcher, Hector) is the other standard tracker and runs on Pi-class
hardware; its known pitfall is sliding along a featureless hallway unless a
penalty pulls it toward the odometry pose.

**Global search from one scan.** The method is correlative scan matching:
score the scan at every candidate pose on a grid of positions and headings
and take the best. Olson made it real time with a coarse-to-fine two level
search ([Olson 2009](https://april.eecs.umich.edu/pdfs/olson2009icra.pdf);
[Olson 2015, M3RSM](https://april.eecs.umich.edu/pdfs/olson2015scanmatch.pdf)
returns the exact brute-force answer ten times faster). Hess et al. made it
fast enough for Cartographer's loop closure with branch-and-bound over
precomputed grids; the score is the sum of map probabilities at the scan's
end points, the window is the whole map for global localization, and
candidates below a minimum score (0.55 local, 0.6 global) are thrown out
([Hess 2016, ICRA](https://research.google/pubs/pub45466)). Both build the
coarse grid by **max-pooling**, not blurring: a max over a window is an
upper bound on every fine pose inside the coarse cell, so the true peak can
never score below a wrong one at the coarse level. The closest thing to what
we need already exists as a reference design: prism_loc's 2D backend (ROS 2
Humble, C++, v0.1 in 2026-07) runs branch-and-bound over the whole map,
keeps the top 8 distinct modes, re-matches them over odometry-chained scans
with a motion gate of 0.3 m or 0.3 rad between counted scans, and commits
only when one hypothesis holds 90 percent of the posterior, else it reports
"ambiguous" and keeps going. 97 of 100 kidnapped-robot queries succeeded,
38 ms per full-map search ([prism_loc](https://github.com/kjungmo/prism_loc),
[its parameters](https://github.com/kjungmo/prism_loc/blob/main/PARAMS.md)).
Too new and Humble-only to depend on, right to copy the shape of.

**Knowing you are lost.** The classic fixes for the kidnapped robot problem
are Sensor Resetting Localization
([Lenser and Veloso](https://www.scitepress.org/Papers/2006/12158/12158.pdf)
summarises it), Mixture MCL ([Thrun et al.](https://www2.cs.sfu.ca/~vaughan/teaching/415/papers/thrun_robust.pdf))
and Augmented MCL, which is what AMCL's `recovery_alpha_*` is. All of them
need a trustworthy "how well does the scan fit here" signal, and the
literature is blunt that overlap-style metrics (ours) are fragile
([Reliable-loc survey](https://arxiv.org/pdf/2411.07815)). The
discriminating signal is the beam model's: compare the range the map
predicts with the range the lidar measured. A measured range longer than
the map allows means the beam went through a wall, which a right pose
cannot do. Its known weakness, and the reason likelihood fields were
invented, is brittleness in angle: a 2 degree heading error at 5 m moves a
beam 17 cm sideways, past a door frame, and reads as "through". The usual
cure is to compare against the nearest prediction among neighbouring beams.

**Robot vacuums.** Public technical detail is thin. Dreame says a new floor
needs its own map and the robot should start on the floor the map belongs
to; the X50 "recognises which floor it is on using landmark detection"
([Dreame multi-floor guide](https://www.dreametech.com/blogs/blog/multi-floor-mapping-robot-vacuum)).
Valetudo's multi-floor setup is manual map loading
([Home Assistant thread](https://community.home-assistant.io/t/multi-floor-vacuuming-using-valetudo-and-dreame-robots/933706)).
The three things vacuums lean on: the dock is a known anchor pose, lift and
wheel-drop sensors tell them they were carried, and a 360 degree scan is
matched against each stored map on wake. We have no dock and choose not to
use a lift signal (see Decisions); the scan match is the part we build.

**Maps that learn.** slam_toolbox's "true lifelong" mode is "highly
experimental" ([README](https://github.com/SteveMacenski/slam_toolbox)).
The research line is Dynamic Pose Graph SLAM
([Walcott-Bryant et al.](https://people.csail.mit.edu/kaess/pub/WalcottBryant12iros.pdf))
and submap replacement ([Zhao et al. 2021](https://arxiv.org/pdf/2111.10946)).
For a one-floor house the simple version is a slow, confidence-gated
occupancy update of the map, which is what a vacuum effectively does when it
"adjusts" after you move a sofa, and matches the dynamic occupancy grid
work (per-cell two-state models, where a cell that keeps flipping is its own
class).

## The goal

A robot that always knows where it is, without being told. It finds its
position on the map from where it stands, without spinning. It keeps
checking that position while it drives or is driven and fixes it when it is
wrong. It knows which saved map it is on. It never calls itself localized
when it is not. Carried, dragged mid-trip, rebooted: it works it out on its
own, and the dashboard tells the truth about what it believes. Later, the
map absorbs changes to the house, and the robot knows when it is on no map
at all.

One principle runs through the design: **contradiction triggers, prior
guides, motion confirms.** Only evidence that contradicts where odometry
says the robot should be starts a search; the search starts from where
odometry says the robot should be; and nothing is believed until a little
motion confirms it. A robot that knew where it was and drove down a
symmetric hallway is not lost: its scan still agrees with the map, and the
prior keeps it on the pose it drove to.

## Requirements

Arlen's, in his order:

1. Localizes without spinning in circles.
2. Keeps localizing while navigating or being driven, unless it is already
   confidently right.
3. Detects which map it is on.
4. Accurate above all: never says localized at a 55 percent match.

Later: learns changes to the map; knows when it is on no map at all.

Agreed with Arlen on 2026-10-27:

- **Everyday changes are not errors.** A moved chair, an open door, a person
  walking past, the dog: the robot stays `tracking` through all of them. The
  judge tolerates things that are closer than the map says (something new in
  the way) and only distrusts things that are farther (a beam through a
  mapped wall), with a tolerance read off recordings. Phase 1 measures
  whether the plain through share separates an open door from a wrong room
  on our floor; the patch rule in "On the shelf" is the fallback.
- **The map is audited before any bar is trusted.** A strict judge punishes
  a correct pose wherever the map is wrong: doubled walls, ghosts of people
  from mapping day, missing glass. Unknown (grey) cells are "no opinion",
  never a wall. Through-beams at known-true poses that cluster on a few
  cells mark map errors to fix or re-map.
- **Numbers to hold it to.** Position within 10 cm and heading within 5
  degrees while `tracking`. A clear candidate within 5 s of the first scan
  after being set down where the spot is unambiguous; a trip asked for then
  leaves at once and its first half metre confirms the pose. Zero false
  `tracking` over twenty carried-and-set-down trials; one miss is a bug, not
  a statistic. Accuracy is measured at three or four taped crosses on our
  floor, each tape-measured from two mapped walls with a taped heading line;
  never from the robot's own pose on a good day. The tape is for testing
  only; a new room needs nothing but a map.
- **A trip waits for the robot, never the other way round.** A trip asked
  for while the robot is unsure is held, not refused, in the waiting slot
  the bridge already has for lidar and Nav2 bring-up. It leaves on its own
  the moment the pose is good enough. A trip stopped mid-drive because the
  pose was contradicted goes back into the same slot: re-find within 30 s
  and it resumes by itself. The 30 s is the localizer's own clock: it starts
  at the first scan with a verdict (not at the request, which would spend it
  on spin-up), or at the stop. A trip may be resumed twice; a third stop, or
  30 s without a re-find, gives the trip up and the voice says so. Two
  resumes exist for the robot stuck on a rug: its wheels turn, odometry
  advances, the world does not, which reads as a contradiction, a re-find at
  the true pose and a resume into the same rug; without a limit that loop is
  silent and endless.
- **Lidar off means belief stale.** Continuous localization exists only
  while the lidar spins. When it has been off, the state is `stale`: the
  last pose is kept, shown softly, and the first scans when the lidar comes
  back re-check it. **The lidar starts on the wake word** (Arlen, changing
  his earlier rule), so by the time "go to the kitchen" has been said the
  judge has a scan and the search has run during spin-up. The wake word
  starts only the lidar (Arlen: "no, only lidar"). Nav2 today follows the
  lidar on its own (the bridge brings it up whenever the lidar scans and a
  pose exists) so it will come up on the wake word too; that is existing
  behaviour, not something this plan adds, and the rail and the remaining
  bring-up pause belong to the lidar power plan, which does not exist yet
  and which this plan depends on. A conversation that triggers no motion
  lets the lidar go off again a minute after it ends.
- **What is allowed while not tracking.** Gamepad driving: yes, always, at
  full speed. A week ago the gamepad ran on cliff and forward sensors alone
  and that was fine; map barriers are a bonus when the pose is known, never
  something the gamepad depends on (Arlen: "lidar is additive to what we
  have"). Barriers follow the pose: in `tracking`, `checking` and `stale`
  clearances are published from it, because a soft pose is still the best
  guess and the barriers mark the steps (the check 30 floor fix, kept). In
  `searching` and `off_map` the pose has been contradicted, so clearances
  are not published and the gamepad runs on sensors as it did before lidar;
  AMCL still has a TF pose in those states, the bridge just skips the
  publish. One accepted wrinkle: a robot carried while asleep publishes
  barriers from its old pose for the few seconds of spin-up, so a robot that
  was parked by the steps may stop once on its first gamepad push in the new
  room; it clears itself as soon as the judge speaks. Trips: leave from
  `tracking` or a clean `checking`, held while `searching`, refused only in
  `off_map`. Emotes and in-place actions: always. Saving a place needs
  `tracking`; the refusal is a line beside the button ("drive me a little
  first", or "wake me first" when `stale`), not a log line.
- **Not on a map needs evidence.** Standing in an unmapped room the honest
  state is `searching` ("nothing found yet"). `off_map` is declared after 60
  s of scans that had a verdict with no candidate surviving on any map. A
  robot whose scans have no verdict (facing unmapped space or dark
  surfaces) is not on its way to `off_map`; it is simply uninformed.
- **The voice speaks only when a human is needed, and only canned lines.**
  After an ordinary idle the robot says nothing and goes. Three sentences
  exist about position, all spoken verbatim, never paraphrased by the
  language model: "give me a second" when a trip is asked for and the robot
  is genuinely searching after being moved; the give-up when a held or
  stopped trip runs out; and "I can't find my place here, I need a map of
  this room" when a trip is asked for in `off_map`. The off-map line is
  never said unprompted. If "give me a second" is ever heard after an
  ordinary idle, that is the pre-trip pause leaking through the speaker, and
  the fix is the lidar and trip rules above, not a nicer sentence. No new
  channel is needed: the first and third are the bridge's reply to the trip
  request (`held` with a reason, or refused with a reason), and the give-up
  is the goal ending `failed` with a reason, which the voice already learns
  from its existing poll. The voice's trip tool speaks the canned line
  itself in each case and tells the language model the line has been said,
  so the robot never talks twice about its position. The
  deafness-during-trips item from the UX audit stays separate.
- **The dashboard's face is a marker and one caption.** Marker solid when
  `tracking`, soft when `checking` or `stale`, gone when `searching` or
  `off_map`. Under the map one plain caption, one string per state: nothing
  while `tracking` or `stale`; "checking my position"; "finding my place"
  ("finding my place, moving helps" when several candidates are tied); "I
  don't know this room. Map it?" with the mapping offer; "lidar off" when
  there is no pose and no scan (after a reboot). The goal row says "held"
  while a trip waits. State words, agree and through shares, the reason
  text, the candidate list and the learner's change overlay live behind the
  existing debug toggle, and so does the Localize button once the search
  runs on its own (phase 3).

Mine, added:

5. **Honest states, not a flag.** The robot reports one of `tracking`,
   `checking`, `searching`, `stale`, `off_map`, and which map; `searching`
   carries a plain-text reason. Every state is earned by evidence; none is
   set as a side effect of a button or a restart.
6. **Right beats fast, but invisible beats both.** A found pose seeds the
   tracker at once but is `checking`, not `tracking`, until the robot has
   moved and the judge still agrees; standing still and re-scoring the same
   scan proves nothing. The motion that confirms is whatever is already
   happening: a trip leaving from a clean `checking`, or a gamepad drive.
   The robot never moves on its own to localize.
7. **Safety follows the pose.** A contradiction mid-trip stops the trip.
   Barriers are published from any pose that has not been contradicted.
8. **Measured first.** Every threshold comes from recorded floor data,
   replayed offline, with the true answer labelled. No bar is picked by
   feel again. Mechanisms whose need has not been shown by a recording stay
   on the shelf until one does.
9. **Fits the Pi 5.** Whole-map search under one second, the judge under
   ten milliseconds per scan, both in numpy (scipy is not on the robot).
10. **Visible to the engineer, invisible to the user.** Behind the debug
    toggle the dashboard shows everything; in front of it a marker and a
    caption.
11. **Honest surface.** One thread in the nav bridge and one module of pure
    functions carry the maths. The plan also touches, and says so: the
    bridge (recorder, judge, state, search thread, held and resumed trips,
    reasons on replies), the voice (speaks three canned lines, adds
    nothing), the dashboard (marker, caption, debug panel, Localize button
    moved into it, Save place gated and its refusal text), the replay tool,
    and AMCL's configuration: `sigma_hit` 0.2 to 0.1 (the number that
    actually caps its accuracy on a 5 cm map), `max_beams` 60 to 120 (180
    makes correlated beams overconfident), the seed covariance from 0.1 m²
    and 0.07 rad² to 0.02 and 0.01 (a 14 cm, 6 degree spread, a little
    wider than the search's own error), and `recovery_alpha_slow`/`fast` to
    zero once the search exists, because two recovery mechanisms fighting
    is the classic failure. Nothing else in the stack changes.

## The design

Four parts; the first exists. The tracker follows, the judge doubts, the
search finds, the learner keeps the map true.

### 1. Tracker: AMCL, configured for our map

AMCL keeps publishing `map -> odom` so Nav2 is never without a frame. It is
re-seeded by us (an `initialpose` with the tight covariance above) whenever
the search commits a pose, and nobody reads its covariance as truth any
more. The configuration changes in requirement 11 are the whole of part 1.

Whether AMCL can hold 10 cm and 5 degrees is measured, not assumed: phase
1's recordings at the taped crosses carry AMCL's pose beside the true one.
If it holds after the configuration change, the tracker question is closed.
If it does not, the scan-to-map refine step from part 3 becomes the tracker
("Refine-step tracker" on the shelf, with its pitfalls written down); that
decision is made at phase 1 exit with the numbers in hand.

### 2. Judge: a strict score and a state machine

The score replaces `scan_fit`. For each of about 360 beams of the latest
scan, from the lidar's map pose at the scan's timestamp (TF at that stamp,
not the latest; at 0.25 m/s half a second of lag is 12 cm), ray-cast across
the map in half-cell steps (so a one-cell diagonal wall cannot be slipped
between) to the first occupied cell, and compare the predicted range with
the measured one. To blunt the beam model's known brittleness in angle, the
predicted range for a beam is the shortest prediction among it and its two
neighbours:

- **agree**: measured within the tolerance of predicted (15 cm plus five
  percent of range to start; read off recordings), or both beyond range.
- **short**: measured well shorter than predicted. Something unmapped is in
  the way. Tolerated; this is what a changing house looks like.
- **through**: measured more than 30 cm plus the tolerance beyond a
  predicted wall that is within 6 m. The beam went through a mapped wall.
  A right pose produces few of these and they land together on a handful
  of cells; the share at true poses is measured in phase 1, not assumed.
- **no opinion**: the ray reaches an unknown cell before any wall, or the
  lidar returned nothing (the RPLidar A1 drops returns on black and matte
  surfaces, so a no-return where the map predicts a wall must never count as
  through). At map load, unknown islands that do not touch the map's border
  are filled as free, so the speckle slam_toolbox leaves inside rooms does
  not swallow beams. A scan whose counted beams are fewer than about a third
  of the total has **no verdict**. A no-verdict scan counts for nothing
  anywhere: it neither passes nor fails a pose and advances no clock. A
  robot facing the open front door is uninformed, not lost.

Two numbers come out of a scan with a verdict: `agree` share and `through`
share, over counted beams. A pose **passes** a scan when agree is above its
bar and through below its bar. A right pose has high agree and low through;
a wrong room has middling agree and lots of through, which is exactly the
case today's score cannot see; the counter has low agree and no through.
Ray casting 360 beams over at most 480 half-cell steps is one vectorised
numpy step array, under 10 ms on the Pi. Scans taken while turning faster
than the bridge's existing `SCAN_FIT_MAX_TURN_RADIANS` are skipped, as
today. The agree tolerance must sit at least twice the tracker's measured
error at the crosses, or an honest 10 cm tracker reads as unsure.

The judge is unhappy in two ways:

1. **Contradiction**: through share over its bar for a few consecutive
   verdict scans. The beams say the robot is somewhere else. The state
   drops to `searching` at once, a running trip is stopped and held, and
   the search runs.
2. **Nothing matches**: agree share under its bar for a sustained stretch
   with through still low. The world does not look like the map here (the
   counter, an unmapped room, a crowd). Not a contradiction, so nothing
   stops and the tracker keeps its pose; the state drops to `checking`
   ("unsure") and the search runs quietly. Case 2 never re-seeds the
   tracker near its current pose: a refined candidate a few centimetres
   from AMCL's estimate will always look a little better, and re-seeding a
   tracking AMCL mid-drive is what lost the robot in floor run 1. Only a
   candidate outside the prior's radius that passes clearly counts, and
   that is treated exactly as a contradiction (stop, `searching`, seed).
   Otherwise agreement comes back as the robot moves and `tracking`
   returns, or the `checking` pose fails as below.

A symmetric hallway is neither: the side and end walls agree, nothing goes
through, and the robot stays `tracking` on odometry. A lidar-on carry is
expected to fire case 1 during the carry itself (beams over the furniture
to walls behind, or walls at the wrong distances); phase 1 records a carry
in a furnished room and a bare one to check, and if either slips past both
cases the anchored scan check comes off the shelf.

The state machine runs on every scan, in localize mode, and owns the only
`localized`-like word anyone else reads:

- `tracking`: the pose has passed a few consecutive verdict scans after the
  robot moved since the pose was handed over.
- `checking`: a pose that has not earned `tracking`: just handed over (boot,
  restart, a search result, the lidar coming back), or demoted by case 2.
  Earned by motion plus agreement, not by time. A **clean** `checking` (no
  failing verdict on the standing scan, and the whole-map search found no
  rival within the margin of the best, prior ignored) may start a trip; the
  trip's first half metre is the confirmation. A trip already running
  continues through `checking`; only `searching` stops one. A `checking`
  pose **fails** when verdict scans keep arriving and it has passed none of
  the last N (about ten, from recordings) or none within 20 s of the first,
  whichever comes first (the counter never moves, so a scan count alone
  would never fail it); or when the search finds a clear winner elsewhere.
  Failing means `searching` ("unconfirmed").
- `searching`: a contradiction fired, or a `checking` pose failed, or there
  is no pose at all (after a reboot). Trips are held, a running trip has
  been stopped and held. The search runs once a second and the state
  carries a reason for the debug panel: "contradiction", "unconfirmed",
  "several candidates", "nothing found yet".
- `stale`: the lidar is off. If there was a pose it is kept and shown
  softly, and the first scans after the wake word enter `checking`. If
  there was none (`stale` entered from `searching`, or a reboot) the marker
  is gone and the caption reads "lidar off".
- `off_map`: 60 s of verdict scans while `searching` with no candidate
  passing on any map. The search keeps running once a second; a clear
  winner on any map leaves `off_map` for `checking`.

What each state allows:

| state | trips | gamepad | emotes, in-place | barriers | save place |
| --- | --- | --- | --- | --- | --- |
| tracking | yes | yes | yes | from the pose, plus sensors | yes |
| checking | start when clean, else held; a running trip continues | yes | yes | from the pose, plus sensors | no ("drive me a little first") |
| stale | held until the first scan | yes | yes | from the last pose, plus sensors | no ("wake me first") |
| searching | held; a running trip is stopped and held | yes | yes | sensors only | no |
| off_map | refused, with the one sentence | yes | yes | sensors only | no |

Thresholds and counts are read off the recordings in phase 1, not guessed.
The bars are stored in `robot_nav.py` next to the pure functions, with the
recorded evidence noted in the comment.

### 3. Search: whole map, one scan, no spin

A RPLidar scan already covers 360 degrees. Spinning the robot adds no
information; it only gave AMCL's particles time to collapse. The search
instead scores one scan at every pose on every saved map:

- Precompute per map two lookup grids from distance-to-nearest-wall turned
  into a score (a likelihood field): a **coarse** one max-pooled over a
  window of about 30 cm (so the true pose can never score below a wrong one
  at the coarse level; a blur would let it), and a **sharp** one at 5 cm
  for refinement. Done once at map load and again whenever the learner
  rewrites the map.
- Beams are capped at 6 m for the search; the house allows it and angular
  error dominates beyond. The coarse pass uses every third beam (120); the
  savings buy a finer heading step.
- Coarse pass: a plain grid of poses every 20 cm and 5 degrees over the
  map's free cells, each scored by summing coarse-grid values at the scan's
  end points. About a thousand free cells times 72 headings times 120 beams
  is 8.6 million numpy gathers, expected 0.3 to 0.6 s on the Pi; measured in
  phase 2 against the one-second bar, with the FFT pass and the coarse
  through term on the shelf if it misses or the true pose does not survive
  it. No ray casting here: ray casting every grid pose is billions of
  samples.
- Keep the top fifty or so distinct peaks (merge peaks within 30 cm and 15
  degrees; the merge radius must not exceed the refine window, or a wrong
  peak a metre from the true pose swallows it). Refine each on the sharp
  grid over ±15 cm and ±10 degrees at 5 cm and 1 degree (about 1500 poses,
  tens of milliseconds in all), then run the ray-cast judge on the fifty
  survivors only, all 360 beams. The coarse score is the same "end points
  near walls" idea as today's defect, so the true pose may not be the
  coarse winner; the judge decides, by agree and through shares, never by
  raw coarse score, and the same shares rank candidates across maps.
- **Rules, written as formulas.** A candidate passes when agree ≥ A and
  through ≤ T. Candidates are ranked by agree − k·through (k about 2; from
  recordings). The **winner is clear** when exactly one candidate passes,
  or the best passing candidate beats the next by at least M. The **prior**
  is the map-to-odom transform frozen at the last `tracking` tick with live
  odometry added since; a candidate is **inside the prior** within one
  fixed radius (about 1 m and 30 degrees, read once off the gamepad-drive
  recording). An inside candidate wins by passing clearly. An outside
  candidate must pass and beat every inside candidate by M; a carried
  robot's inside candidates all fail, so the far one wins at once. The
  prior is ignored for the clean-`checking` rival test, or a robot carried
  to a look-alike corner near its old spot would pass clean.
- **No carried state, no lift sensor, no detector.** Arlen: "Do I know
  where I am? Is there another point that makes more sense? covers drag,
  carry, everything." It is one search, not two: the whole map is always
  scored, the prior only decides how much evidence a candidate needs, so a
  carry costs nothing extra. The search runs at most once a second. A seed
  from a mid-air scan is harmless: it enters `checking`, the wheels do not
  turn in the air so it cannot earn `tracking`, and the next search a
  second later overwrites it.
- **Believe, then confirm by motion.** A clear winner is committed at once:
  seed AMCL, enter `checking`. `checking` becomes `tracking` only after the
  robot has moved at least 0.3 m or 0.3 rad (the prism_loc motion gate; a
  stationary robot can confirm nothing) and the judge still agrees. If no
  winner is clear the state stays `searching` ("several candidates"), the
  trip stays held, and the search simply runs again a second later; as the
  robot moves, one fresh scan from a new spot breaks the tie, and the
  winner is committed then. No candidate tracking across scans, no nudge:
  the robot never moves itself to localize. A tie nothing resolves runs out
  the held trip's 30 s and the give-up is spoken; the caption says moving
  helps.
- **Which map.** The search runs on every saved map, read from the PGM
  files on disk (`/map` carries only the loaded one), and the best
  confirmed candidate names the map. Switching maps is what it is today
  (the bridge asks map_server to load the other file and re-seeds AMCL),
  decided by evidence instead of by trying maps in turn.
- **Not on a map.** No candidate passes on any map for 60 s of verdict
  scans: `off_map`, as above. On the counter the search may roughly match
  the kitchen walls with every beam short; agree stays low there and
  nothing passes.

Where it lives: a worker thread in the nav bridge, with its own flag, not
the bridge's `busy` activity lock that relocalize uses today: an activity
refuses trips, pauses the state machine and resets the pending goal's
clock every tick, all of which the trip rules above forbid. The bridge
already owns the scan, the pose, odometry, TF, the trip state and
`nav_update`; the state is written straight into `nav_update`, which the
dashboard and voice already read. The pending goal gets a `held` state
with the localizer's own 30 s clock and a resume count; a contradiction
moves a running goal back into that slot instead of cancelling it. The
search takes up to a second and numpy releases the GIL for its big
gathers, but whether the half-second tick and the barrier publisher stall
is measured in phase 2; if they do, the search moves to a subprocess. All
the maths is pure numpy functions in `robot_nav.py` (or a sibling module if
it outgrows the file) with tests on synthetic and recorded scans.

### 4. Learner: the map follows the house

Arlen: "If I move a chair or close a door or remove a coffee table, the map
gets updated and persisted. groundfloor is groundfloor. I don't want a
million different iterations of the same map."

Designed in outline now, built last, re-planned at phase 3 exit with the
measured judge numbers in hand.

- **One map, changed in place.** `groundfloor.pgm` and its yaml are the
  map. The learner changes cells and writes them back (temporary file, then
  rename, when the robot is idle). Beside it, not maps the user chooses
  between: the evidence counters (`groundfloor.evidence.npz`) and one
  pristine copy (`groundfloor.original.pgm`) made the first time the
  learner runs on a map that has none, and by the save-map path for maps
  made later. The copy exists because a re-map has a new origin and kills
  every place and barrier; with it, a bad lesson is one click in the Map
  card, "forget what you learned", shown only when there is something to
  forget and with a count. The map's origin and size never move, so places
  and barriers are untouched.
- **A fourth kind of cell, sometimes.** Seen solid on some visits and open
  on others (a door, a chair that moves daily). Stored as a second grey the
  map server reads as unknown, so Nav2 plans through it with caution and
  the live scan paints it into the costmap when it is there; the judge and
  the search treat it as no opinion. A sometimes cell that then reads the
  same way for ten consecutive visits settles back to that state, so a
  removed door does not stay a hole in the judge's view forever. (Checked:
  a mid-grey PGM value reads as -1 in the occupancy grid; the costmap plans
  through unknown with cost; AMCL's likelihood field ignores it.)
- **Votes only from disagreeing beams.** Only while `tracking` with high
  agree, only cells within 4 m, only beams hitting the predicted wall at
  more than 20 degrees from grazing, only while slow or still. A through
  beam votes **free** on the wall cell it passed through; a short beam
  votes **occupied** on the cell it stopped in; an agreeing beam votes
  nothing. This is what stops the learner eating itself: a pose 10 cm off
  for a whole lap casts no votes at all, because 10 cm is inside the agree
  tolerance, whereas raw votes from every beam would have walked a wall 10
  cm in three laps and the tracker after it.
- **Visits and verdicts.** All the votes a cell receives until it has had
  none for 30 minutes are one visit. A visit has a verdict on a cell only
  when it is clear: at least 20 votes, at least 90 percent one way. A cell
  changes after three separate visits with a clear verdict against it and
  none for it in between (defaults accepted by Arlen). A cell that changes
  and later changes back becomes sometimes.
- **Who reads the new map.** The bridge asks map_server to reload by the
  `load_map` service it already uses; `/map`, the costmap and the lookup
  grids follow. AMCL clears its laser models on a reload and may
  re-initialise; the reload therefore waits for idle and is followed by a
  re-seed from the current pose, measured on the robot in phase 4.

For the three examples: a removed coffee table fades to free after three
visits and the judge stops seeing a contradiction there (its tolerance
covers it meanwhile); a moved chair fades at the old spot and appears at
the new one, and one that moves daily becomes sometimes; a door closed on
mapping day becomes sometimes after its first open visit and never
surprises the judge or the search again.

### What this gives Arlen's four points

1. No spin: one scan, whole map, candidates in under a second, confirmed by
   the first half metre of the trip or whatever drive is already happening.
2. Continuous: the judge runs on every scan in every mode of driving; a
   contradiction or a world that stops matching starts a search without a
   button, from where odometry says the robot should be. A symmetric
   hallway driven from a known spot disturbs nothing.
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
- **Three states instead of five.** `searching`, `stale` and `off_map`
  share a permissions row, but they mean different things to the person
  looking at the marker, and five cost one enum and one caption each.
- **A separate `robot-localizer` systemd unit.** Approved, then withdrawn:
  the search's one need, not stalling the tick, is a thread (or at worst a
  subprocess) inside the bridge; a unit would add a topic, a relay into
  `nav_update` and a second process to deploy and watch.
- **Tracking candidates across odometry-chained scans** (the full prism_loc
  posterior). A multi-hypothesis tracker's worth of code to break ties that
  one fresh search from a new spot breaks anyway in a house.
- **A one-turn nudge** for tied candidates. Cut by Arlen; the robot never
  moves itself to localize.
- **A gamepad speed cap while unsure.** Struck by Arlen: lidar is additive.
- **Ghost candidates on the map** while searching. The caption says moving
  helps; the candidate list is in the debug panel.

On the shelf, built only when a recording shows the simpler thing failing:

- **The patch rule** for through-beams: group them into connected patches by
  angle, drop the largest patch or two, count the leftover, as a share of
  beams never a width in metres. Comes in if the open-door recording pushes
  a true pose's through share toward a wrong pose's.
- **The edge rule**: ignore beams whose predicted hit is within two cells of
  a wall end or an unknown cell. Comes in if the neighbour-minimum
  prediction leaves true-pose through shares noisy.
- **A coarse through term**: look up two points along each beam (at 60 and
  85 percent of its range) in a dilated wall grid and subtract hits, so the
  coarse pass has a cheap version of the judge's question. Triples the
  gathers. Comes in if a labelled true pose ever fails to reach the top
  fifty coarse peaks in replay.
- **An FFT correlation pass** for the coarse search (72 headings, hit image
  correlated with the coarse grid, padded to 512 cells so a 6 m beam cannot
  wrap; about 0.75 s at 512 on the robot, extrapolated from a 0.17 s test at
  256). Comes in if the plain grid misses the one-second bar.
- **AMCL global init with 50 000 particles** as the search of last resort if
  the numpy search cannot be made to fit the Pi.
- **An anchored "scan moved while the wheels did not" check**: during a
  still period keep the first scan and compare each new scan against it
  (scan to scan misses a slow drag); a large change means the robot moved
  without its wheels; "wheels moved, scan did not" is stuck on a rug. Comes
  in if the phase 1 carry recordings slip past both judge cases.
- **A seed gate on the cliff sensors reading floor**, so a search never
  seeds mid-air. Vacuums use lift sensors for this. Not needed while a
  mid-air seed is harmless (it cannot earn `tracking` and is overwritten a
  second later); written down because it is one line if that ever changes.
- **The refine-step tracker**: the sharp-grid refine run on every scan
  around the odometry-propagated pose, replacing AMCL. Cartographer's
  real-time matcher, in effect. Needs a penalty toward the odometry pose or
  it slides along hallways, a window scaled to the distance driven since
  the last scan, sub-cell steps to meet 10 cm and 5 degrees, and its own
  future-stamped `map -> odom` broadcaster at 10 Hz kept alive through
  `stale`. Comes in only if AMCL, configured as in requirement 11, misses
  10 cm or 5 degrees at the taped crosses in phase 1.
- **A dock or boot anchor.** A known parking spot gives a free, strong prior
  at boot, the way vacuums use the dock. After the learner, if ever.

## Phases

Each phase ends with something Arlen can see on the floor or in a replay.
There is no separate measuring phase: the recorder lands first in phase 1
and recordings accumulate during the floor tests we already do. The evening
counts carry the lesson of the first floor evening (far smaller changes
produced three fix batches).

### Phase 1: the judge (five to six evenings)

- A `record` command in the bridge saves the latest scan, the lidar's map
  pose from TF at the scan's stamp and odometry to a JSON lines file under
  `~/.config/robot-pet/recordings/`. Truth comes from three or four taped
  crosses, each tape-measured from two mapped walls with a taped heading
  line; a recording at a cross carries that pose as its label, elsewhere
  Arlen's sentence is the label. Tonight's wrong room and the metre-off
  pose are the first two entries. Further recordings: true pose at the
  crosses and the places, right spot wrong heading, on the counter, a carry
  with the lidar on through a furnished room and a bare one, a drag along
  the hallway, a gamepad drive around the floor while correctly localized
  (as a sequence, for the prior radius and the motion gate), and a drive
  past the open door and the dark sofa.
- Time the lidar from power on to first scan, once, and write the number
  here; today's 20 s is a waiting allowance in the code, not a measurement.
- `tools/localization_replay.py` prints today's `scan_fit` and the new
  `agree` and `through` shares for every recording, and AMCL's error at
  each cross. That table goes into this document and the bars are read off
  it, with margin. Pass/fail for the judge: the worst true-pose through
  share must sit clearly below the best wrong-pose share across all
  recordings, with the open-door recording counted as a true pose. If it
  does not, the patch rule comes off the shelf first, then the edge rule;
  if neither separates them, this plan stops and is rethought rather than
  tuned. The carry recordings decide the anchored check; the cross
  recordings decide the tracker (AMCL holds 10 cm and 5 degrees, or the
  refine-step tracker comes off the shelf for phase 2).
- Map audit: the replay draws where through-beams land at the known-true
  poses. Clusters on a few cells are map errors: fix those cells or re-map
  (doors in their usual state, nobody walking, slow drive). Spread
  everywhere means the tolerance is wrong, not the map. The verdict on
  `groundfloor` goes into this document before any bar is set.
- `robot_nav.beam_check(...)`: the ray cast at half-cell steps,
  neighbour-minimum prediction, no-return and unknown as no opinion,
  unknown-island fill at map load, the no-verdict rule; pure numpy, tests on
  synthetic maps and on the recordings, which run in the normal suite.
- The state machine in the bridge: five states, both judge cases change
  state (phase 3 wires them to the search), handed poses enter `checking`,
  `checking` fails by the N-scans-or-20-s rule, `localized` becomes "state
  is tracking or clean checking" (clean without the rival test until phase
  2 provides a search). A contradiction cancels a running trip through the
  gate that already exists. In `searching` and `off_map` the barrier
  publish is skipped. Save place needs `tracking`, with its refusal text.
  Until phase 2 a trip asked for while `searching` still runs today's
  relocalize spin, as it does now when not localized, with the judge now
  rejecting wrong rooms; this phase must not make a lost robot worse.
- AMCL: `sigma_hit` 0.1, `max_beams` 120, seed covariance 0.02 and 0.01.
- Dashboard: the marker (solid, soft, gone) and the caption in front; state
  word, shares, map name behind the debug toggle.
- Exit, on the floor: a metre off and the wrong room are never shown as
  `tracking`; a correct pose reaches `tracking` within three scans after a
  seed and a short drive; the counter drops to `checking` then `searching`
  within 20 s; a hand-dragged robot drops to `searching`; a robot parked
  nose to a wall or under the desk still leaves on a trip at once; a
  correctly localized robot driven by gamepad through the hallway and into
  corners, past a moved chair, an open door and the dark sofa, stays
  `tracking` the whole way; AMCL's error at the crosses is written down.

### Phase 2: the search (three to four evenings)

- `robot_nav.global_search(...)`: the two lookup grids (max-pooled coarse,
  sharp fine), the coarse pass at 20 cm and 5 degrees on 120 beams, peak
  merging at 30 cm and 15 degrees, top fifty, sharp refinement, judge on
  the survivors, the pass/clear/prior formulas. Timed on the robot against
  the one-second bar, and the tick and barrier publisher watched for stalls
  while it runs. Tests on recordings: every labelled true pose must be in
  the top fifty coarse peaks and must be the clear winner after the judge;
  the wrong room recording must not produce a passing candidate at the
  wrong room; the counter recording must produce none.
- The rival test for a clean `checking`, prior ignored.
- The 0.3 m / 0.3 rad motion gate between a seed and `tracking`.
- Relocalize becomes "search now" on its own thread, not an activity: spin
  code deleted, `RELOCALIZE_SPINS` gone. The Localize button stays in front
  only until phase 3 gives the search its own triggers.
- All saved maps are searched; the winner names the map.
- Exit, on the floor: carried to twenty spots, set down, Localize pressed:
  the right candidate within 5 s where the spot is unambiguous, `tracking`
  within the first half metre of a trip, never a wrong room, zero misses;
  at the crosses, within 10 cm and 5 degrees; a search on the counter stays
  `searching`, never committed; a tie (the two matching corners of the
  living room, if there are any) breaks within a metre of gamepad driving.

### Phase 3: always on (three to four evenings)

- The two judge cases run the search: contradiction (loud, trip stops and
  is held), nothing matches (quiet, no near re-seed, a far clear winner
  treated as a contradiction). Boot with no remembered pose and a failed
  `checking` pose also search. Once a second at most.
- The pending goal grows a `held` state with the localizer's 30 s clock
  (started at the first verdict scan, or at the stop) and a resume count of
  two; a contradiction moves the running goal into it instead of
  cancelling; the trip leaves on its own from `tracking` or a clean
  `checking`. `off_map` refuses with its reason. The reply to a trip
  request carries `held` or the refusal reason; the give-up ends the goal
  `failed` with its reason. The voice's trip tool speaks the three canned
  lines from those fields and tells the language model they were said.
- The lidar starts on the wake word; `stale` when it goes off. The
  dashboard goal row shows "held".
- `off_map` after 60 s of verdict scans with nothing passing on any map;
  the search keeps running there.
- AMCL `recovery_alpha_slow` and `recovery_alpha_fast` set to zero; the
  search owns recovery now.
- The Localize button moves behind the debug toggle.
- Exit, on the floor: say "go to the kitchen" from idle: the robot leaves
  with no pause from localization and no wiggle, and says nothing (the
  bring-up pause that remains is the lidar power plan's). Drag the robot
  mid-trip two metres and turn it: the trip stops within two scans, the
  state goes `searching`, it re-finds itself and resumes, and the marker
  and caption tell the story without a button press. Carry it to another
  room: same, minus the resume. Put it on the counter and ask for a trip:
  `searching`, then `off_map` after a minute, the one sentence, no trip.
  Lidar off, carry it, wake word: `stale`, `checking`, a search, never the
  old pose shown as tracking. Park it on the rug so it slips: the trip
  stops, resumes, stops, resumes, and the third stop is the give-up line,
  not silence. A drive past the dark sofa and through the hallway at speed
  never stops a trip. Nothing about position is ever said twice.

### Phase 4: the learner (re-planned at phase 3 exit; about four evenings)

- `robot_nav.map_votes(...)` (votes from one scan's disagreeing beams at a
  tracking pose, reusing the judge's ray cast) and
  `robot_nav.apply_visits(...)` (visits, verdicts, the three-visit rule, the
  sometimes state and its settling). Tests on synthetic maps: a removed box
  fades after three visits, not two; a flipping door becomes sometimes and
  settles after ten steady visits; a wall under a constant 10 cm pose
  offset never moves; nothing changes from a `checking` or `searching`
  pose. The lookup grids are rebuilt after every map write.
- The learner runs in the bridge, writes the PGM and the evidence file
  atomically when idle, asks map_server to reload, and re-seeds AMCL after
  the reload, whose behaviour is measured on the robot first. The original
  copy is made on first run if missing, and by the save-map path
  thereafter.
- Map card: "forget what you learned", with a count, shown only when there
  is something to forget; the change overlay is in the debug panel only.
- Exit, on the floor: remove the coffee table; after three visits on
  separate occasions it is gone from the map and the judge's through share
  there is back to zero. Open a door that was closed when mapped, then
  close it: the door cells read as sometimes. Localization accuracy is
  unchanged across the phase; the map's origin, places and barriers are
  untouched.

Total: eleven to fourteen evenings for localization that just works, plus
about four for the learner.

## Decisions

The settled list. The dated history of how each was reached is in git
(`git log -- "docs/plans/2026-10-26 - localization-plan.md"`).

Arlen's, by his word:

- No carry or drag detection of any kind; "do I know where I am, is there
  another point that makes more sense" covers everything. (The cliff seed
  gate on the shelf is a sanity line, not a detector, and is not built.)
- Everyday changes never cost `tracking`; a door's through-beams in one
  patch are "something changed", not a contradiction, as long as the rule
  is a common pattern and not janky.
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
  learned" as the correction; three visits, 30 minute visit gap (defaults
  accepted).
- No separate measuring evening; recordings come from normal floor tests.
- Dashboard words in front are Claude's call.

Claude's, as final arbiter after four review rounds:

- AMCL stays the tracker, configured for a 5 cm map; the refine-step
  tracker is on the shelf, decided by AMCL's measured error at the crosses.
- Two judge cases, not three; the anchored scan check is on the shelf until
  a carry recording slips past both.
- No-verdict scans count for nothing anywhere.
- Case 2 never re-seeds near the current pose.
- The search is a thread in the bridge with its own flag, not an activity
  and not a unit; the pending goal gets a `held` state, a 30 s clock from
  the first verdict scan, and two resumes.
- Max-pooled coarse grid, 5 degree steps on 120 beams, merge at 30 cm,
  fifty survivors, formulas for pass, clear and prior; ties are broken by
  re-running the search, not by tracking candidates.
- Three canned lines ride the existing trip reply and poll; the voice tool
  speaks them and the language model adds nothing.
- Barriers from any pose that has not been contradicted; skipped in
  `searching` and `off_map`.
- Marker plus one caption in front; everything else behind debug.
- Learner votes only from disagreeing, non-grazing beams within 4 m;
  sometimes cells settle after ten steady visits; built last, re-planned at
  phase 3 exit.

## Sources

- [Olson 2009, Real-time correlative scan matching](https://april.eecs.umich.edu/pdfs/olson2009icra.pdf), [Olson 2015, M3RSM](https://april.eecs.umich.edu/pdfs/olson2015scanmatch.pdf)
- [Hess et al. 2016, Real-Time Loop Closure in 2D LIDAR SLAM](https://research.google/pubs/pub45466)
- [prism_loc, 2D MCL with branch-and-bound global relocalization and verification](https://github.com/kjungmo/prism_loc), [parameters](https://github.com/kjungmo/prism_loc/blob/main/PARAMS.md)
- [Nav2 AMCL configuration](https://docs.nav2.org/jazzy/configuration_and_development/configuration_guide/others/configuring_amcl/), [ReinitializeGlobalLocalization BT node](https://docs.nav2.org/configuration/packages/bt-plugins/actions/ReinitializeGlobalLocalization.html)
- [Beluga AMCL](https://github.com/Ekumen-OS/beluga), [ROSCon 2024 slides](https://roscon.ros.org/2024/talks/Beluga_AMCL-_A_modern_Monte_Carlo_Localization_implementation_for_ROS.pdf)
- [slam_toolbox README](https://github.com/SteveMacenski/slam_toolbox), [JOSS paper](https://joss.theoj.org/papers/10.21105/joss.02783.pdf), [ROSCon 2019 slides](https://roscon.ros.org/2019/talks/roscon2019_slamtoolbox.pdf)
- [Cartographer tuning](https://google-cartographer-ros.readthedocs.io/en/latest/tuning.html), [going further](https://google-cartographer-ros.readthedocs.io/en/latest/going_further.html)
- [Thrun et al., Robust Monte Carlo localization (Mixture MCL)](https://www2.cs.sfu.ca/~vaughan/teaching/415/papers/thrun_robust.pdf); Thrun, Burgard, Fox, Probabilistic Robotics, ch. 6.3 (beam model and its brittleness)
- [Sensor Resetting Localization summary](https://www.scitepress.org/Papers/2006/12158/12158.pdf)
- [Reliable-loc, survey of localization failure detectors](https://arxiv.org/pdf/2411.07815), [Nubert et al., learned localizability](https://arxiv.org/pdf/2203.05698)
- [NVIDIA Isaac localization failure detection](https://docs.nvidia.com/isaac/doc/extensions/localization/doc/index.html)
- [Free-space features for global localization](https://arxiv.org/abs/1908.01863)
- [Dreame multi-floor guide](https://www.dreametech.com/blogs/blog/multi-floor-mapping-robot-vacuum), [Dreame forum](https://forum.dreametech.com/forum.php?mod=viewthread&tid=1261), [Valetudo multi-floor thread](https://community.home-assistant.io/t/multi-floor-vacuuming-using-valetudo-and-dreame-robots/933706)
- [Walcott-Bryant et al., Dynamic Pose Graph SLAM](https://people.csail.mit.edu/kaess/pub/WalcottBryant12iros.pdf), [Zhao et al. 2021, lifelong localization and mapping framework](https://arxiv.org/pdf/2111.10946), [BIM-based 2D lidar localization](https://arxiv.org/abs/2308.05443v1)
- [ROS Answers, slam_toolbox vs AMCL](https://answers.ros.org/question/365472/), [2026 vehicle paper keeping AMCL](https://arxiv.org/pdf/2604.24242)
