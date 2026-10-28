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
that we are missing: a **tracker** (AMCL, kept for now) plus a **judge** (a
strict score that counts beams passing *through* mapped walls, and a
confirmation step over a little motion before anything is believed) plus a
**search** (match one full 360 degree scan against the whole map, every
saved map, by brute-force correlation, no spinning) plus, once those three
hold on the floor, a **learner** that keeps the one map true as the house
changes. The judge runs all the time; the search runs whenever the judge is
unhappy. That one question, "do I know where I am, and is there another spot
that makes more sense", covers drags, carries and reboots alike. No lift
sensors, no carried state, no detection of anything. From Arlen's chair the
whole thing should be invisible: the lidar wakes with the wake word, a trip
leaves without a pause or a wiggle, and the robot speaks only when it has
genuinely been moved somewhere it cannot place. One thread in the nav
bridge, one module of pure functions, five states, three phases plus the
learner.

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
guides, motion confirms.** Only evidence that contradicts where odometry
says the robot should be (beams through mapped walls, or a world that no
longer matches the map at all) starts a search; the search starts from
where odometry says the robot should be; and nothing is believed until a
little motion confirms it. A robot that knew where it was and drove down a
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

Agreed with Arlen on 2026-10-27, after asking what would make him say "why
doesn't this work the way I expected":

- **Everyday changes are not errors.** A moved chair, an open door, a person
  walking past, the dog: the robot stays `tracking` through all of them. The
  judge tolerates things that are closer than the map says (something new in
  the way) and only distrusts things that are farther (a beam through a
  mapped wall), with a tolerance read off recordings. An open door that was
  closed on mapping day makes a small cluster of through-beams; a wrong room
  makes through-beams everywhere. Phase 1 measures whether the plain through
  share already separates those two on our floor. If it does not, the ready
  fallback is to group through-beams into connected patches by angle, drop
  the largest patch or two, and count what is left; that rule is a share of
  beams, never a width in metres, because a coffee table is wider than a
  door. This is the same tolerance for unexplained beams every particle
  filter carries, made explicit. The learner (design part 4) is what keeps
  this true over months.
- **The map is audited before any bar is trusted.** A strict judge punishes
  a correct pose wherever the map is wrong: doubled walls from an imperfect
  loop closure, ghosts of people or open doors from mapping day, missing
  glass or dark surfaces. Unknown (grey) cells are "no opinion", never a
  wall. Through-beams at known-true poses that cluster on a few cells mark
  map errors to fix or re-map; through-beams spread everywhere mean the
  judge's tolerance is wrong. Re-map with care if needed: doors in their
  usual state, nobody walking, slow drive with loop closures.
- **Numbers to hold it to.** Position within 10 cm and heading within 5
  degrees while `tracking`. A clear candidate within 5 s of the first scan
  after being set down where the spot is unambiguous; a trip asked for then
  leaves at once and its first half metre confirms the pose. Zero false
  `tracking` over twenty carried-and-set-down trials; one miss is a bug, not
  a statistic. The twenty trials measure misses; accuracy is measured at
  three or four taped crosses on the floor, each measured with a tape from
  two mapped walls (wall cells are known to 5 cm from the map) and given a
  taped heading line. Never from the robot's own pose on a "good day", which
  the floor tests show is 10 to 20 cm off at best. The tape is for testing
  on our floor only; a new room needs nothing but a map.
- **A trip waits for the robot, never the other way round.** A trip asked
  for while the robot is unsure is held, not refused, in the same waiting
  slot the bridge already uses for lidar and Nav2 bring-up. It leaves on its
  own the moment the pose is good enough. A trip stopped mid-drive because
  the pose was contradicted uses the same slot: re-find within 30 s with the
  goal still standing and it resumes by itself. If 30 s pass either way, the
  trip is given up and the voice says so. One rule for trips not yet
  started and trips interrupted.
- **Lidar off means belief stale.** Continuous localization exists only
  while the lidar spins. When it has been off, the state is `stale`: the
  last pose is kept, shown softly, and the first scans when the lidar comes
  back re-check it. Nothing detects a carry while the lidar is off; the
  re-check is what notices. **The lidar starts on the wake word** (Arlen,
  2026-10-27, changing his earlier rule), so by the time "go to the kitchen"
  has been said the judge already has a scan and the search has already run
  during spin-up; the localizer adds no pause between the request and the
  first wheel turn. The wake word starts only the lidar (Arlen, 2026-10-27:
  "no, only lidar"); the power rail and Nav2 still come up for the trip, and
  that pause belongs to the lidar power plan, which does not exist yet and
  which this plan depends on. A conversation that triggers no motion lets
  the lidar go off again a minute after it ends. Emotes and in-place actions
  never wait for a map fix.
- **What is allowed while not tracking.** Gamepad driving: yes, always, at
  full speed. A week ago the gamepad ran on cliff and forward sensors alone
  and that was fine; map barriers are a bonus when the pose is known, never
  something the gamepad depends on (Arlen, 2026-10-27: "lidar is additive to
  what we have"). Barriers follow the pose, not the state: whenever there is
  a map pose at all (`tracking`, `checking`, `stale`) barrier clearances are
  published from it, because a soft pose is still the best guess and the
  barriers mark the steps; only `searching` and `off_map`, where there is no
  pose, fall back to sensors alone. This is the floor fix from check 30 kept,
  not undone. Trips: leave from `tracking` or a clean `checking`, held while
  `searching`, refused only in `off_map`. Emotes and in-place actions: yes.
  Saving a place needs `tracking`; a place saved a metre off is a lasting
  annoyance.
- **Not on a map needs motion.** Standing still in an unmapped room the
  honest state is `searching` ("nothing found yet"). `off_map` is declared
  after about 2 m of travel or 60 s of trying with no candidate surviving on
  any map.
- **The voice speaks only when a human is needed, and only canned lines.**
  After an ordinary idle the robot says nothing and goes. Three sentences
  exist about position, all spoken verbatim from the bridge, never
  paraphrased by the language model: "give me a second" when a trip is
  asked for and the robot is genuinely searching after being moved; the
  give-up when a held or stopped trip runs out its 30 s; and "I can't find
  my place here, I need a map of this room" when a trip is asked for in
  `off_map`. The off-map line is never said unprompted: a robot resting in a
  rearranged room announcing that it does not know the room is the worst
  sentence to hear in a mapped house. If "give me a second" is ever heard
  after an ordinary idle, that is the pre-trip pause leaking through the
  speaker, and the fix is the lidar and trip rules above, not a nicer
  sentence. Today the voice learns trip outcomes only by polling, so the
  give-up needs a small event path from the bridge to the voice; that is
  the one piece of the UX audit's cluster A this plan builds (phase 3). The
  deafness-during-trips item stays separate.
- **The dashboard's face is a marker and one caption.** Marker solid when
  `tracking`, soft when `checking` or `stale`, gone when `searching` or
  `off_map`. Under the map one plain caption, no numbers, one string per
  state: nothing while `tracking` or `stale`; "checking my position";
  "finding my place" ("finding my place, moving helps" when several
  candidates are tied); "I don't know this room. Map it?" with the mapping
  offer; "waiting for lidar" after a reboot before the first scan. State
  words, agree and through shares, the reason text, the candidate list and
  the learner's change overlay live behind the existing debug toggle, and so
  does the Localize button once the search runs on its own (phase 3): useful
  to the engineer, and a "find yourself" button in front says the robot
  cannot be trusted to.

Mine, added:

5. **Honest states, not a flag.** The robot reports one of `tracking`,
   `checking`, `searching`, `stale`, `off_map`, and which map; `searching`
   carries a plain-text reason. Every state is earned by evidence; none is
   set as a side effect of a button or a restart. Five, not three, because
   they cost one enum, one table row each and one caption each, and `stale`
   versus `searching` versus `off_map` mean different things to the person
   looking at the marker. (Arlen cut a longer list: "reduce to the ones
   that actually convey useful information".)
6. **Right beats fast, but invisible beats both.** Committing to a wrong
   pose is worse than taking ten more seconds. A found pose seeds the
   tracker at once but is `checking`, not `tracking`, until the robot has
   moved and the judge still agrees; standing still and re-scoring the same
   scan proves nothing. The motion that confirms is the trip itself: a trip
   may leave from a clean `checking`, and its first half metre is the test.
   The robot never moves on its own to localize; a tie waits for the next
   drive or runs out the 30 s.
7. **Safety follows the pose.** Trips start from `tracking` or a clean
   `checking` (both shares pass on the standing scan, and no rival candidate
   on any map comes within a margin of the best, with the prior ignored for
   that test). A contradiction mid-trip stops the trip. Barriers are
   published whenever a pose exists.
8. **Measured first.** Every threshold comes from recorded floor data,
   replayed offline, with the true answer labelled. No bar is picked by
   feel again. Mechanisms whose need has not been shown by a recording stay
   in "Alternatives considered" until one does.
9. **Fits the Pi 5.** Whole-map search under one second, the judge under
   ten milliseconds per tick, both in numpy (scipy is not on the robot).
10. **Visible to the engineer, invisible to the user.** Behind the debug
    toggle the dashboard shows the state, the shares, the candidates and
    the learner's changes, so a wrong belief can be seen before it is driven
    on. In front of it there is a marker and a caption.
11. **Honest surface.** One thread in the nav bridge and one module of pure
    functions carry the maths. The plan also touches, and says so: the
    bridge (recorder, judge, state, search thread, held and resumed trips,
    three canned lines as events), the voice (speaks those events
    verbatim), the dashboard (marker, caption, debug panel, Localize button
    moved into it, Save place gated on `tracking`), the replay tool, and
    AMCL's configuration: the seed covariance tightens from today's 0.1 m²
    in position (a 32 cm spread, useless for a 10 cm target) and 0.07 rad²
    in heading (15 degrees) to about 0.02 and 0.01; `max_beams` goes from
    60 to 180, which costs nothing and matters for a 10 cm target; and
    `recovery_alpha_slow`/`fast` go to zero once the search exists, because
    two recovery mechanisms fighting (AMCL sprinkling particles while we
    seed) is the classic failure. Nothing else in the stack changes; the
    motion node is untouched.

## The design

Four parts; the first exists. The tracker follows, the judge doubts, the
search finds, the learner keeps the map true.

### 1. Tracker: AMCL for now, with its replacement running in shadow

AMCL keeps publishing `map -> odom` so Nav2 is never without a frame. It is
re-seeded by us (an `initialpose` with the tight covariance above) whenever
the search commits a pose, and we stop reading its covariance as truth.

The search's refine step (part 3) is already a tracker: a window of about
20 cm and 10 degrees around the odometry-propagated pose, some 1500 poses
scored by lookup per tick, tens of milliseconds on the Pi. From phase 2 it
runs every tick in shadow beside AMCL, against a sharp lookup grid (5 cm,
not the half-metre blur the coarse search uses), and both poses are logged
against the taped crosses. At phase 3 exit, if the shadow holds 10 cm and 5
degrees across the floor tests, it replaces AMCL outright: one
`map -> odom` broadcaster, and the seeding, the covariance, the recovery
fight and AMCL's map-reload behaviour (which the learner would otherwise
have to work around) all go in one change. If it does not hold, AMCL stays
and nothing was lost but a log column. Decided before the learner, not
after, because the learner's hardest unknown exists only while AMCL is the
tracker.

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
  assumed.
- **no opinion**: the beam reaches an unknown cell before any wall, or the
  lidar returned nothing for it. The RPLidar A1 drops returns on black and
  matte surfaces, so a no-return where the map predicts a wall must never
  count as through, or every dark sofa and TV stand becomes a false
  contradiction. A scan whose counted beams are fewer than about a third of
  the total (facing an open front door, an unmapped room) gives no verdict,
  like today's `SCAN_FIT_MIN_BEAMS`; a verdict-less stretch is handled by
  the state machine below, not ignored.

Two numbers come out: `agree` share and `through` share, both over counted
beams. A right pose has high agree and low through; a wrong room has
middling agree and lots of through, which is exactly the case today's
score cannot see; the counter has low agree and no through. If phase 1's
recordings show an open door pushing a true pose's through share up toward
a wrong pose's, the patch rule from Requirements (drop the largest connected
patches, count the leftover) replaces the plain share. Ray casting 360 beams
over at most 240 steps of 5 cm is one vectorised numpy step array, well
under 10 ms on the Pi. Scans taken while turning faster than the bridge's
existing `SCAN_FIT_MAX_TURN_RADIANS` are skipped, as today: at 0.8 rad/s one
7.5 Hz scan is skewed about six degrees and would read as through
everywhere.

The judge is unhappy in two ways, and both run the same search (part 3):

1. **Contradiction**: through share high for a few consecutive ticks. The
   beams say the robot is somewhere else. The state drops to `searching`
   at once and a running trip stops, because driving on a contradicted
   pose is the one thing that can hurt.
2. **Nothing matches**: agree share low for a sustained stretch, or no
   verdict for a sustained stretch. The world does not look like the map
   here (the counter, an unmapped room, a crowd), or there is nothing to
   compare (facing glass, an open front door). Not a contradiction, so
   nothing stops; the state drops to `checking` ("unsure") and the search
   runs quietly from the prior. If a candidate elsewhere clearly beats the
   current pose, the robot re-seeds there; if agreement comes back as the
   robot moves, `tracking` returns; if neither happens, `checking` fails
   as described below.

A symmetric hallway is neither: the side walls and end walls agree, nothing
goes through, and the robot stays `tracking` on odometry. Arlen's "hey now
we're 60%, maybe we should be silently relocalizing" is case 2.

What a lidar-on carry looks like to the judge: a robot lifted to hand height
sees over its mapped furniture to the walls behind, which is through-beams
at every mapped sofa and table; a robot walked across a room while its
belief stays put sees walls at the wrong distances, which is through-beams
too. The expectation is that case 1 fires during the carry itself. Phase 1
records a lidar-on carry to check that; if the recording shows the carry
slipping past both cases, the anchored scan comparison in "Alternatives
considered" comes back. Not before.

The state machine runs every tick, in localize mode, and owns the only
`localized`-like word anyone else reads:

- `tracking`: both shares pass for a few consecutive ticks after the robot
  has moved since the pose was handed over. The "nothing matches" tolerance
  belongs here, because this pose earned its prior by driving to it.
- `checking`: a pose that has not earned `tracking`: just handed over
  (boot, restart, a search result, the lidar coming back), or demoted from
  `tracking` by case 2. Earned by motion plus agreement, not by time. A
  **clean** `checking` (both shares pass on the standing scan, and the
  whole-map search found no rival within a margin of the best, prior
  ignored) may start a trip; the trip's first half metre is the
  confirmation and the existing gate stops it if the judge disagrees. A
  `checking` pose **fails** when both shares have not passed within N
  counted scans after the robot moved (N from recordings, about ten), or
  when the search finds a clear winner elsewhere; failing means
  `searching`. Replaces today's unconditional yes.
- `searching`: a contradiction fired, or a `checking` pose failed. A trip
  in progress is stopped and held; a trip asked for is held. The search
  runs (part 3) and the state carries a reason in plain text for the debug
  panel: "contradiction", "unconfirmed", "several candidates", "nothing
  found yet".
- `stale`: the lidar is off, so nothing has checked the pose since it went
  off. The last pose is kept and shown softly; the first scans after the
  wake word enter `checking`. After a reboot there is no last pose, so the
  state is `searching` ("nothing found yet") with the caption "waiting for
  lidar" until the first scan.
- `off_map`: the search found no acceptable candidate on any saved map and
  the robot has since moved about 2 m, or tried for 60 s, so it should have.

What each state allows:

| state | trips | gamepad | emotes, in-place actions | barriers | save place |
| --- | --- | --- | --- | --- | --- |
| tracking | yes | yes | yes | from the pose, plus sensors | yes |
| checking | yes when clean, else held | yes | yes | from the pose, plus sensors | no |
| stale | held until the lidar's first scan | yes | yes | from the last pose, plus sensors | no |
| searching | held, running trip stopped | yes | yes | sensors only | no |
| off_map | refused, with the one sentence | yes | yes | sensors only | no |

The user sees the marker: solid (tracking), soft (checking, stale), gone
(searching, off_map), and the one caption.

Thresholds and tick counts are read off the recordings in phase 1, not
guessed. The bars are stored in `robot_nav.py` next to the pure functions,
with the recorded evidence noted in the comment.

### 3. Search: whole map, one scan, no spin

A RPLidar scan already covers 360 degrees. Spinning the robot adds no
information; it only gave AMCL's particles time to collapse. The search
instead scores one scan at every pose on every saved map:

- Precompute per map two lookup grids (distance-to-nearest-wall turned into
  a score, like a likelihood field): one blurred to about half a metre so a
  coarse heading step cannot miss the true peak, one sharp at 5 cm for
  refinement and the shadow tracker. Done once at map load and again
  whenever the learner rewrites the map.
- Beams are capped at about 6 m for the search. The house allows it and it
  drops the noisiest returns.
- Coarse pass: a plain grid of poses every 20 cm and 10 degrees over the
  map's free cells, each scored by summing blurred-grid values at the
  scan's end points. About a thousand free cells times 36 headings times
  360 beams is 13 million numpy gathers, 0.2 to 0.4 s on the Pi, and it
  never scores a pose inside a wall or off the map. No ray casting here:
  ray casting every grid pose would be about three billion samples and is
  dead on arrival on a Pi. (An FFT correlation pass was the alternative and
  is in "Alternatives considered"; it is built only if the plain grid
  misses the one-second bar.)
- Keep the top twenty or so distinct peaks (merge peaks within a metre and
  20 degrees), refine each at 1 degree and one cell on the sharp grid
  (about 1300 poses each, tens of milliseconds in all), then run the
  ray-cast judge on the twenty survivors only. The coarse score is the same
  "end points near walls" idea as today's defect, so the true pose may not
  be the coarse winner; the judge decides, by agree and through shares,
  never by raw coarse score, and the same shares rank candidates across
  maps.
- **The prior guides.** When the search starts from `tracking` or
  `checking` the robot has a prior: the map-to-odom transform frozen at the
  last `tracking` tick, with live odometry added since. Not AMCL's live
  estimate, which wanders once it is lost. The prior's doubt grows with the
  distance driven since (a few percent of the distance, a few degrees per
  turn; the numbers come from the gamepad-drive recordings in phase 1).
  Candidates inside that doubt need little evidence to win; candidates
  outside it need strong evidence over motion. The prior never decides
  whether a `checking` pose is clean: that rival test ignores it, or a
  robot carried to a look-alike corner near its old spot would pass clean.
- **No carried state, no lift sensor, no detector.** Arlen: "Do I know
  where I am? Is there another point that makes more sense? covers drag,
  carry, everything." If the robot was moved by hand, the candidates near
  the prior fail the judge and the far candidates win instead. It is one
  search, not two: the whole map is always scored, the prior only decides
  how much evidence a candidate needs, so a carry costs nothing extra.
  While the lidar is on and the robot is in the air, the search must not
  fire on every mid-air scan and must never seed from one: it runs at most
  once a second and only once two consecutive scans agree with each other.
- **Believe, then confirm by motion.** If one candidate clearly beats the
  rest it is committed at once: seed AMCL, enter `checking`. `checking`
  becomes `tracking` only after the robot has moved and the judge still
  agrees; standing still and re-scoring the same scan proves nothing, and
  AMCL does not update until the robot has moved either. The motion is
  whatever is already happening: a teleop drive, or the trip itself leaving
  from a clean `checking`. If two or more candidates remain close the state
  stays `searching` ("several candidates"), the trip stays held, and the
  search re-matches the survivors as the robot moves; each
  odometry-chained scan either strengthens or kills a candidate (the
  prism_loc pattern). Not two spins, and no nudge: the robot never moves
  itself to localize. A tie that nothing resolves runs out the held trip's
  30 s and the give-up is spoken; the caption says moving helps.
- **Which map.** The search runs on every saved map and the best confirmed
  candidate names the map. Switching maps is what it is today (restart
  map_server and AMCL on the new one) but decided by evidence, not by trying
  maps in turn.
- **Not on a map.** No candidate survives the judge on any map, the robot
  has moved about 2 m or tried for 60 s so the confirmation step has had its
  chance, and the judge still fails: `off_map`. Until then the state is
  `searching` ("nothing found yet"). Once declared, the marker goes and the
  caption offers mapping; the voice says so only if a trip is asked for. The
  search reads every saved map from disk (the PGM files), not from `/map`,
  which only ever carries the loaded one. On the counter the search may
  roughly match the kitchen walls (walls are vertical) with every beam
  short; the judge's agree share stays low there and nothing is committed.

Where it lives: a worker thread in the nav bridge, the way relocalize
already runs (`start_activity`). The bridge already owns the scan, the
pose, odometry, TF, the trip state and `nav_update`; the search can take up
to a second and numpy releases the GIL, so the half-second tick does not
stall. The state is written straight into `nav_update`, which the dashboard
and voice already read; no new topic, no relay, no second process to deploy
and watch. (A separate `robot-localizer` unit was approved on 2026-10-26
and withdrawn on 2026-10-27; see Decisions.) All the maths is pure numpy
functions in `robot_nav.py` (or a sibling module if it outgrows the file)
with tests on synthetic and recorded scans.

### 4. Learner: the map follows the house

Arlen: "If I move a chair or close a door or remove a coffee table, the map
gets updated and persisted. groundfloor is groundfloor. I don't want a
million different iterations of the same map."

This part is designed now and built last. Its thresholds cannot be chosen
before the judge exists, because "learn only while agreement is high" needs
a measured "high". It is re-planned at phase 3 exit with those numbers and
with the tracker decision made.

**One map, changed in place.** `groundfloor.pgm` and its yaml are the map,
the same files mapping wrote. The learner changes their cells and writes
them back (to a temporary file, then renamed over the original, so a crash
mid-write cannot leave half a map) when the robot goes idle. No learned
copy, no versions, one map the user ever sees. Under the hood two files sit
beside it that are not maps the user chooses between: the evidence
counters, and one pristine copy (`groundfloor.original.pgm`) made the first
time the learner runs on a map that has none, and by the save-map path for
maps made later. The copy exists because the obvious correction, "re-map
the room", does not work: a new map from slam_toolbox has a new origin and
frame, so every place and barrier dies with it. With the copy, a bad lesson
is one click in the Map card, "forget what you learned", shown only when
something has been learned and with a count. Arlen: "if it helps to have a
recent history or something, sure". Places and barriers are otherwise
unaffected: they live in map coordinates, and the map's origin and size
never move; only cells change.

**Four kinds of cell.** The PGM already has occupied (black), free (white)
and unknown (grey). The learner adds **sometimes**: a cell that has been seen
solid on some visits and open on others, which is what a door, a gate or a
chair that moves daily looks like. It is stored as a second grey the map
server reads as unknown, so Nav2 plans through it with caution and the live
scan paints it into the costmap when it is actually there. The judge treats
sometimes like unknown: no opinion, never a contradiction; this means the
judge has no opinion on beams toward a closed door even though the room
beyond is mapped, which is acceptable. The search's lookup grid ignores it.
Nothing else in the stack needs to know. (Checked: a mid-grey PGM value
reads as -1 in the occupancy grid; the costmap's unknown handling plans
through it with cost; AMCL's likelihood field ignores unknown.)

**What counts as evidence.** Only while `tracking` with a high agree share,
only cells within about 4 m of the lidar (ray accuracy falls off with
distance), only while moving slowly or standing. Votes come only from the
judge's own disagreeing beams, never from every beam. A through-beam (the
map says wall, the beam went well past it) votes **free** on the mapped
cell it passed through; a short beam (the map says open, the beam stopped
well before) votes **occupied** on the cell it stopped in; an agreeing beam
votes nothing. This matters: a pose that is 10 cm off for a whole lap, which
is what AMCL's error looks like within one visit, casts no through or short
votes, because 10 cm is inside the agree tolerance. Raw per-cell votes from
every beam would have let that steady offset move a wall 10 cm in three
laps and the tracker follow it. Votes are grouped into **visits**: all the
votes a cell receives until it has received none for 30 minutes. One lap of
the kitchen is one visit, however many scans it took. A visit has a verdict
on a cell only when it is clear: at least 20 votes, at least 90 percent one
way. A cell changes state after three separate visits (default, accepted by
Arlen) have a clear verdict against it with none for it in between. A cell
that changes state and later changes back becomes sometimes, and stays so.
The counters live in one sidecar file beside the map
(`groundfloor.evidence.npz`), which is bookkeeping, not another map.

**What this does for the three examples.**

- Coffee table removed: on three visits the beams pass through its cells;
  they fade to free; from then on the judge sees no contradiction there. In
  the meantime the judge's tolerance covers them.
- Chair moved: its old cells fade to free as above; its new cells are hit
  on three visits and become occupied. A chair that moves daily never
  settles and becomes sometimes, which is honest.
- Door: solid one day, open the next. The judge's tolerance keeps the robot
  `tracking` while it looks at the open door, which is what lets the
  learner see the door at all. After one flip it is sometimes, and neither
  the judge nor the search is ever surprised by it again.

**Who reads the new map.** The bridge already asks map_server to load a map
file by its `load_map` service (it does this today for map switching and
for the keepout mask). After a write the learner asks for the same reload;
map_server republishes `/map`, and the costmap and the search pick it up. If
AMCL is still the tracker at that point, it clears its laser models on a
reload and, depending on version, re-initialises from its configured
initial pose, which is the origin; the reload then waits for idle and is
followed by a re-seed from the current pose. If the shadow tracker has
replaced AMCL, the reload is a lookup-grid rebuild and nothing else.

**Why it does not eat itself.** Learning from a slightly wrong pose smears
the map and a smeared map makes the next pose slightly wrong. The guards:
votes only from beams that clearly disagree with the map, so a steady small
pose error produces no votes at all; evidence only while `tracking` with
high agreement; a clear per-visit verdict; three separate visits before any
change; no change at all while the state is anything else; and the
sometimes state as the honest answer for cells that keep arguing. The debug
panel can show what changed, for the engineer; the user is not shown a sofa
as "changed" when the robot already handles it. If a lesson is wrong,
"forget what you learned" restores the mapping-day original and keeps
places and barriers.

### What this gives Arlen's four points

1. No spin: one scan, whole map, candidates in under a second, confirmed
   by the first half metre of the trip or whatever drive is already
   happening.
2. Continuous: the judge runs every tick in every mode of driving; a
   contradiction or a world that stops matching starts a search without
   anyone pressing a button, from where odometry says the robot should be.
   A symmetric hallway driven from a known spot still agrees with the map
   and disturbs nothing.
3. Which map: the search scores all maps and picks the one that survives.
4. Accurate: nothing is called `tracking` without a high agree share, a low
   through share, and motion the judge agreed through. A 55 percent match
   of any kind is `searching`, and the marker says so.

## Alternatives considered

Rejected:

- **Move the bars, or weight AMCL's covariance in.** No. Wrong room scored
  inside the true range; no bar fixes a score that does not measure the
  right thing.
- **Replace AMCL with slam_toolbox localization mode.** More precise
  tracking, possibly, but no global search, a re-map into pose graphs, heavy
  tuning, and it would still need parts 2 and 3.
- **Cartographer pure localization.** Has the global search built in, but
  not packaged for Lyrical beyond messages, Lua configuration, maintenance
  mode. No.
- **Adopt prism_loc.** Right shape, but Humble, C++, three months old. We
  copy its verification pattern and keep our stack.
- **Keep spinning but spin smarter.** Spinning is a workaround for a search
  tool that is not a search tool. Removing it is the point.
- **Write our own particle filter.** Not needed; the tracker is the part
  that works, and the shadow refine tracker is simpler than a filter.
- **Three states instead of five.** `searching`, `stale` and `off_map`
  share a permissions row, but they mean different things to the person
  looking at the marker, and five cost one enum and one caption each. Kept
  at five.

Ready on the shelf, built only when a recording shows the simpler thing
failing:

- **An anchored "scan moved while the wheels did not" check.** While
  odometry reports no motion, keep the first scan of the still period and
  compare each new scan against it (scan to scan misses a slow drag: 7 cm
  per scan at walking pace is inside tolerance); a large change means the
  robot was moved by something other than its wheels; "wheels moved, scan
  did not" is stuck on a rug and must not search. A person walking past
  changes about 8 percent of beams, a carry nearly all. Cut because the
  carry into a small room is already "nothing matches" and the lidar-on
  carry is expected to fire "contradiction" on its own; the one case it
  alone would catch, a slow drag along a symmetric hallway, is one the
  search cannot resolve either. Comes back if the phase 1 carry recording
  slips past both judge cases.
- **The patch rule** for through-beams (drop the largest connected patches,
  count the leftover). Comes in if the open-door recording pushes a true
  pose's through share toward a wrong pose's.
- **An FFT correlation pass** for the coarse search: for each of 72 headings
  rotate the scan's end points into a hit image and correlate with the
  blurred grid, padded to 512 cells a side so a 6 m beam cannot wrap
  (228 + 241 and 170 + 241 both fit; 12 m would not). About 0.75 s at 512
  on the robot, extrapolated from a 0.17 s test at 256. Built only if the
  plain grid misses the one-second bar.
- **A one-turn nudge** for tied candidates. Cut by Arlen; the plan's own
  text admitted a small turn only catches flukes. A tie waits for the next
  drive or runs out the held trip's 30 s.
- **Ghost candidates on the map** while searching. The caption says
  "finding my place, moving helps"; the candidate list is in the debug
  panel.
- **A separate `robot-localizer` systemd unit.** Approved, then withdrawn:
  the one reason for it, the search must not stall the tick, is solved by
  the bridge's existing worker thread, and the unit would have added a
  topic, a relay into `nav_update` and a second process to deploy.
- **A dock or boot anchor.** A known parking spot gives a free, strong prior
  at boot, the way vacuums use the dock. After the learner, if ever.

## Phases

Each phase ends with something Arlen can see on the floor or in a replay.
There is no separate measuring phase: the recorder lands first in phase 1
and recordings accumulate during the floor tests we already do. The
evening counts are honest guesses with the lesson of the first floor
evening in them (far smaller changes produced three fix batches).

### Phase 1: the judge (three to four evenings)

- A `record` command in the bridge saves the latest scan, the lidar's map
  pose from TF, odometry and AMCL covariance to a JSON lines file under
  `~/.config/robot-pet/recordings/`. Truth comes from taped crosses on the
  floor: three or four spots, each measured with a tape from two mapped
  walls and given a taped heading line, the coordinates written on the
  tape. A recording at a cross carries that pose as its label; elsewhere
  Arlen's sentence is the label. Tonight's wrong room and the metre-off
  pose are the first two entries. Further recordings: true pose at the
  crosses and the places, right spot wrong heading, on the counter, a carry
  with the lidar on, a drag along the hallway, a gamepad drive around the
  floor while correctly localized (as a sequence, so the prior's doubt
  numbers and the motion-confirmation step can be replayed), and a drive
  past the open door and the dark sofa.
- Time the lidar from rail on to first scan, once, and write the number
  here; today's 20 s is a waiting allowance in the code, not a measurement,
  and the trip's 30 s start allowance is sized around it.
- `tools/localization_replay.py` prints today's `scan_fit` and the new
  `agree` and `through` shares for every recording. That table goes into
  this document and the bars are read off it, with margin. Pass/fail for
  the judge itself: the worst true-pose through share must sit clearly
  below the best wrong-pose share across all recordings, with the open-door
  recording counted as a true pose. If it does not, the patch rule comes
  off the shelf first, then an edge rule (ignore beams whose predicted hit
  is within two cells of a wall end or an unknown cell); if neither
  separates them, this plan stops and is rethought rather than tuned. The
  carry recording decides whether the anchored scan check comes off the
  shelf.
- Map audit: the replay also draws where through-beams land at the
  known-true poses. Clusters on a few cells are map errors: fix those cells
  or re-map (doors in their usual state, nobody walking, slow drive). Spread
  everywhere means the tolerance is wrong, not the map. The audit's verdict
  on `groundfloor` goes into this document before any bar is set.
- `robot_nav.beam_check(...)`: the ray-cast score with no-return and
  unknown as no opinion and the minimum counted share, pure numpy, tests on
  synthetic maps and on the recordings, which run in the normal suite.
- The state machine in the bridge: five states, both judge cases change
  state (phase 3 wires them to the search), handed poses enter `checking`,
  `checking` fails after N scans, `localized` becomes "state is tracking or
  clean checking" (clean without the rival test until phase 2 provides a
  search). A contradiction cancels a running trip (the gate already exists,
  it just reads a better input). Barriers published whenever a pose exists.
  Save place needs `tracking`.
- AMCL seed covariance tightened to 0.02 and 0.01; `max_beams` to 180.
- Dashboard: the marker (solid, soft, gone) and the caption in front; state
  word, shares, map name behind the debug toggle.
- Exit, on the floor: a metre off and the wrong room are never shown as
  `tracking`; a correct pose reaches `tracking` within three ticks of a
  seed and a short drive; the counter drops to `checking` then `searching`
  within the chosen ticks; a hand-dragged robot drops to `searching`; a
  correctly localized robot driven by gamepad through the hallway and into
  corners, past a moved chair, an open door and the dark sofa, stays
  `tracking` the whole way. The relocalize spin still exists in this phase
  and now has a judge that rejects wrong rooms.

### Phase 2: the search (three to four evenings)

- `robot_nav.global_search(...)`: the two lookup grids, the plain coarse
  pass, peak picking, sharp refinement, judge on the survivors, timed on
  the robot against the one-second bar. Tests on recordings: every
  labelled true pose must be the top confirmed candidate; the wrong room
  recording must not produce a confirmed candidate at the wrong room; the
  counter recording must produce none.
- The rival test for a clean `checking`: no other candidate within a
  margin of the best, prior ignored.
- Confirmation over odometry-chained scans with the motion gate; tied
  candidates stay `searching` with the trip held.
- The shadow tracker: the refine step every tick around the odometry pose
  on the sharp grid, logged beside AMCL's pose; the replay compares both
  against the crosses.
- Relocalize becomes "search now" on the bridge's worker thread: spin code
  deleted, `RELOCALIZE_SPINS` gone. The Localize button stays in front only
  until phase 3 gives the search its own triggers.
- All saved maps are searched; the winner names the map.
- Exit, on the floor: carried to twenty spots, set down, Localize pressed
  (phase 3 moves the button): the right candidate within 5 s where the
  spot is unambiguous, `tracking` within the first half metre of a trip,
  never a wrong room, zero misses; at the crosses, within 10 cm and 5
  degrees; a search on the counter stays `searching`, never committed.

### Phase 3: always on (two to three evenings)

- The two judge cases run the search: contradiction (loud, trip stops),
  nothing matches (quiet, from the prior, switch only on a clear winner).
  Boot with no remembered pose and a `checking` pose that fails also
  search. The search runs at most once a second and never seeds from a
  scan that disagrees with the one before it.
- The lidar starts on the wake word; the search runs during spin-up; a
  trip asked for in a clean `checking` leaves at once. `stale` when the
  lidar goes off. The trip's start allowance is sized from the measured
  spin-up.
- Trips held, not refused: a trip asked for while `searching` or in an
  unclean `checking` waits in the existing slot and leaves on its own;
  a trip stopped by a contradiction resumes from the same slot after a
  re-find within 30 s; either way 30 s ends in the give-up. `off_map`
  refuses with its one sentence, only when asked.
- `off_map` after the search fails on every map and the robot has moved
  about 2 m or tried for 60 s.
- The three canned lines travel as events from the bridge to the voice and
  are spoken verbatim; the voice's trip-refusal paraphrase goes.
- AMCL `recovery_alpha_slow` and `recovery_alpha_fast` set to zero; the
  search owns recovery now.
- The Localize button moves behind the debug toggle.
- Exit, on the floor: say "go to the kitchen" from idle: the robot leaves
  with no pause from localization and no wiggle, and says nothing (the
  rail and Nav2 bring-up that remain are the lidar power plan's to remove).
  Drag the robot mid-trip two metres and turn it: trip stops within two
  ticks, state goes `searching`, re-finds itself and resumes the trip, and
  the marker and caption tell the story without a button press. Carry it to
  another room: same, minus the resume. Put it on the counter and ask for a
  trip: `searching` then `off_map`, the one sentence, no trip. Lidar off,
  carry it, wake word: `stale` then `checking` then a search, never the old
  pose shown as tracking. A drive past the dark sofa and through the
  hallway at speed never stops a trip. Shadow tracker verdict: holds 10 cm
  and 5 degrees across all of the above, or does not; the swap decision is
  written into this document.

### Phase 4: the learner (re-planned at phase 3 exit; about four evenings)

- `robot_nav.map_votes(...)`: the per-tick votes from one scan's disagreeing
  beams at a tracking pose, reusing the judge's ray cast.
  `robot_nav.apply_visits(...)`: the visit grouping, the per-visit verdict
  and the three-visit rule, with the sometimes state. Tests on synthetic
  maps: a removed box fades after three visits, not two; a flipping door
  becomes sometimes; a wall under a constant 10 cm pose offset per visit
  never moves; nothing changes from a `checking` or `searching` pose. The
  lookup grids are rebuilt after every map write.
- The learner runs in the bridge (it already has the scan, the pose and the
  state), keeps the counters, writes the PGM and the evidence file
  atomically when idle, and asks map_server to reload. The original copy is
  made on first run if missing, and by the save-map path thereafter.
- If AMCL is still the tracker: its reaction to a map reload is measured on
  the robot and the reload is gated on idle plus a re-seed.
- Map card: "forget what you learned", with a count, shown only when there
  is something to forget; the change overlay is in the debug panel only.
- Exit, on the floor: remove the coffee table; after three visits on
  separate occasions the table is gone from the map and the judge's through
  share at that spot is back to zero. Open a door that was closed when
  mapped, then close it: after the flip the door cells read as sometimes.
  Localization accuracy is unchanged across the whole phase; the map's
  origin, places and barriers are untouched.

Total: eight to eleven evenings for localization that just works, plus
about four for the learner.

## Decisions

A dated record. Where a later entry contradicts an earlier one (the nudge,
the lidar rule, the undo copy, the Localize button, trips only in
`tracking`, the separate unit, the extra trigger), the later entry is the
decision.

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
- The lidar spin-up is timed, not assumed.

2026-10-27, after the second review (asked to disagree with decisions, and
measured against Arlen's "it should just work, like an Apple device";
Arlen: "yes, take all"):

- The lidar starts on the wake word, not on the trip. This changes Arlen's
  earlier rule, by his decision: "Fine, we can start lidar on wake word."
- A trip may leave from a clean `checking`; its first half metre confirms
  the pose. No pre-trip wiggle.
- No-return beams and beams into unknown are no opinion; too few counted
  beams is no verdict; scans taken while turning fast are skipped.
- The search runs at most once a second and never seeds from a scan that
  disagrees with the previous one.
- Coarse and refine passes use the lookup grid, never ray casts; beams
  capped at 6 m; the ray-cast judge runs on the twenty survivors only.
- AMCL seed covariance about 0.02; `recovery_alpha_*` to zero in phase 3.
- Ground truth: crosses tape-measured from two mapped walls with a heading
  line.
- One hidden mapping-day original beside the map, because a re-map kills
  places and barriers; "forget what you learned" is the correction. Per
  visit verdict (20 votes, 90 percent); the ten-visit return rule is cut.
- Localize button behind the debug toggle from phase 3 (Arlen: "keep
  localize behind a debug toggle"). The user's dashboard is the marker;
  state words, shares, reasons, candidate lists and the change overlay live
  behind the debug toggle.
- The gamepad is never slowed: the review suggested a speed cap while the
  pose is unsure, it went in under "take all", and Arlen struck it the same
  day ("a week ago we didn't have lidar at all, and gamepad was fine. lidar
  is additive"). The lidar staying on a minute after a conversation was
  confirmed as fine.
- Evening estimates doubled. "Tracker swap" means the refine step run
  every tick, replacing AMCL, not Beluga.

2026-10-27, after the third round (three reviewers: UX, soundness,
simplicity; Arlen answered the nine open questions and delegated the rest:
"You are the final arbiter"):

- Arlen: the one-turn nudge is cut; auto-resume of a stopped trip stays;
  the wake word starts only the lidar, so phase 3 promises "no pause from
  localization" and the rail and Nav2 bring-up are named as the lidar power
  plan's; five states stay unless they add real complexity (they do not);
  dashboard words in front are Claude's call.
- Claude's calls, folded in: the learner stays in this plan as design, is
  built last and re-planned at phase 3 exit with measured numbers; the
  "scan moved while wheels did not" trigger is cut to the shelf, with the
  anchored version written down, until the carry recording shows a miss;
  the localizer is a thread in the bridge, not a unit, withdrawing the
  2026-10-26 decision; the refine step runs in shadow beside AMCL from
  phase 2 and replaces it at phase 3 exit if it holds 10 cm and 5 degrees;
  one plain caption under the marker.
- Agreed by all three reviewers and taken: barriers follow the pose, so
  they stay on in `checking` and `stale` (the check 30 fix kept); a trip
  asked for while unsure is held in the existing slot, not refused, one
  rule with the mid-trip resume; `checking` fails after N scans without
  both shares passing, and the "nothing matches" tolerance belongs to
  `tracking` only; a clean `checking` ignores the prior for its rival test,
  and a tie that nothing resolves runs out the 30 s and speaks the give-up;
  the off-map sentence is spoken only in answer to a trip request; the
  three voice lines are canned and travel as events from the bridge, the
  one piece of audit cluster A this plan builds; Save place needs
  `tracking`; the plain grid is the coarse pass and the FFT goes to the
  shelf; the patch rule goes to the shelf until the open-door recording
  needs it; a sustained no-verdict stretch demotes to `checking`; the
  seed's heading variance is 0.01 and `max_beams` is 180; learner votes
  come only from disagreeing beams, with a constant-offset test, and the
  learner writes only when idle; the original copy is made on first run;
  a visit ends when a cell has had no votes for 30 minutes; the "20 s with
  a nudge" target is gone with the nudge; after a reboot the state is
  `searching` with the caption "waiting for lidar"; ghost candidates are
  cut in favour of the caption; evening estimates set at eight to eleven
  plus about four for the learner.

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
