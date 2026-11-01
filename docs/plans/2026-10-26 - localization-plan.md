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

Our localizer has one real defect: the scan fit score cannot tell a right
pose from a wrong one. It only asks "does each beam end near a wall", and in
a house of walls a wrong room scores 0.69 and a pose a metre off scores 0.55.
AMCL itself is fine as a tracker once it is handed a true pose.

The fix is the structure every working system has and we are missing:

- a **tracker** (AMCL, kept, configured for our map),
- a **judge** (a strict score that counts beams passing *through* mapped
  walls, which a right pose almost never does),
- a **search** (one 360 degree scan matched against every saved map by brute
  force, no spinning),
- **confirmation by motion** (nothing is trusted until a little driving
  agrees),
- later, a **learner** that keeps the one map true as the house changes.

The judge asks "does this pose contradict the map?" every tick. The search
asks "is there another spot that makes more sense?" That covers drags,
carries, reboots and wrong seeds without detecting any of them.

This plan is the skeleton. Every bar is read off recordings of our own floor
in phase 1. A list of **fixes to try** holds the refinements a design review
proposed, each with the recording that would justify building it. Nothing on
that list is built until a recording shows the problem.

## Where we are and why it fails

Today, in localize mode, AMCL tracks the pose and the nav bridge computes
`scan_fit`: the share of beams whose end lands within 15 cm of a mapped wall.
A boolean `localized` flips at 0.4 and 0.6. A remembered pose at boot sets
`localized` without checking anything. Relocalize scatters AMCL's particles
over the map and spins the robot up to twice.

Floor numbers from this weekend:

| situation | scan fit |
| --- | --- |
| true pose, various spots | 0.55 to 0.94 |
| right spot, wrong heading | 0.44 to 0.49 |
| a metre off, dashboard said localized | 0.55 |
| wrong room, relocalize accepted it | 0.69 |
| standing on the counter | 0.23 |

The wrong room scores inside the true range, so no bar can fix this score.
"End near a wall" is satisfied by most poses in a rectangular house. The
question that separates right from wrong is "does a beam pass through a wall
the map says is solid?"

Smaller defects ride along. 5000 particles over a whole floor is too thin
for a global search, which is why it needs spins and still lands wrong.
AMCL corrects only every 25 cm or 12 degrees of travel. And the bridge reads
AMCL's belief late, because AMCL post-dates its `map -> odom` transform by
`transform_tolerance` (1 s).

## How people actually do this

Every system that relocalizes reliably has three parts: a tracker for when
the pose is roughly right, a global search that finds candidates from one
scan without moving, and a confirmation step before a candidate is believed.

- **Trackers.** Nav2's AMCL is the standard for a prior map
  ([docs](https://docs.nav2.org/jazzy/configuration_and_development/configuration_guide/others/configuring_amcl/)).
  Beluga and slam_toolbox's localization mode are alternatives; neither is
  packaged for Lyrical with a global search (checked on the robot
  2026-10-27).
- **Global search from one scan** is correlative scan matching: score the
  scan at every pose on a grid and keep the best. Olson made it real time
  with a max-pooled coarse grid
  ([Olson 2009](https://april.eecs.umich.edu/pdfs/olson2009icra.pdf)); Hess
  et al. used the same idea for Cartographer's loop closure, with a heading
  step chosen so the farthest beam moves at most one cell
  ([Hess 2016](https://research.google/pubs/pub45466)). Max-pooling makes the
  coarse cell an upper bound on every pose inside it, so the true pose
  cannot be lost to the coarse level; wrong cells can still outscore it, so
  several candidates go forward. The closest reference design is prism_loc
  (ROS 2 Humble, 2026): whole-map branch-and-bound, top modes re-checked
  over a few motion-gated scans before committing
  ([prism_loc](https://github.com/kjungmo/prism_loc)). We copy its shape,
  not its code.
- **Knowing you are lost** needs a score that measures contradiction, which
  is the beam model's question: did a beam go further than the map allows?
  Its known weakness is angle (Probabilistic Robotics ch. 6.4): 2 degrees of
  heading error moves a beam at 5 m by 17 cm, past a door frame. The cure is
  to compare each beam against its neighbours' predictions too. No
  contradiction score can see a pose that agrees with the map in the wrong
  place (a slip along a corridor, a half turn in a plain room). Only a
  better pose elsewhere reveals it.
- **Robot vacuums** have a dock as an anchor, lift sensors, and a 360 degree
  scan matched against each stored map on wake. We have no dock and choose
  not to use a lift signal; the scan match is the part we build.
- **Maps that learn** update a cell only from poses the localizer accepts,
  and only slowly, with clamped evidence counting confirmations as well as
  contradictions, so one bad pass cannot flip it. slam_toolbox's lifelong
  mode is still marked experimental.

## Requirements

Arlen's:

1. Localizes without spinning in circles.
2. Keeps localizing while navigating or driven.
3. Detects which map it is on.
4. Accurate above all: never says localized at a 55 percent match.

Later: learns changes to the map; knows when it is on no map at all.

Agreed with Arlen on 2026-10-27:

- Everyday changes (a moved chair, an open door, a person, a removed coffee
  table) never cost the pose. One patch of beams going through where the map
  drew something is "something changed", not a contradiction.
- Unknown on the map is never treated as a wall. The map is audited before
  any bar is trusted.
- Within 10 cm and 5 degrees while tracking. Zero false "tracking" over
  twenty carry trials; one miss is a bug.
- A trip waits for the robot to find itself, then leaves on its own. A trip
  stopped by a lost pose resumes on its own. 30 s is the most a trip waits.
- Lidar off means the pose is stale. The lidar starts on the wake word.
- The gamepad is never slowed or blocked. Map barriers are a bonus over the
  cliff and forward sensors: "lidar is additive to what we have".
- The voice speaks only when a human is needed, in its own words (changed
  2026-10-30 from fixed lines).
- The dashboard shows a marker and one caption; detail lives behind the
  debug toggle, the Localize button too once the search runs on its own.

Mine:

5. **Honest states, not a flag.** Every state is earned by evidence, never
   set by a button or a restart.
6. **The robot never moves itself to localize.** Confirmation comes from
   motion that is already happening: a trip leaving, a gamepad drive.
7. **Measured first.** Every bar comes from recorded floor data, replayed
   offline with the true answer labelled.
8. **Fits the Pi 5.** numpy only (no scipy on the robot). A search under two
   seconds per map; the judge under fifty milliseconds a tick.

## The design

### 1. Tracker: AMCL, configured for our map

AMCL keeps publishing `map -> odom` so Nav2 always has a frame. The search
re-seeds it with an `initialpose`. Changes, checked at taped crosses in
phase 1:

- `update_min_d` and `update_min_a` 0.25 and 0.2 to 0.1 and 0.1, so it
  corrects every 10 cm or 6 degrees.
- `sigma_hit` 0.2 to 0.1, a sharper beam weight on a 5 cm map (0.15 if the
  particles collapse).
- Seed covariance 0.1 and 0.07 to 0.02 and 0.01, about the search's own
  error.
- **The bridge carries the seed forward.** A search takes up to two
  seconds, and AMCL's own handling plants the seed as the pose *now* on a
  live robot (the "failed to transform initial pose in time" warning). So
  the bridge moves the found pose forward by the odometry since the scan
  before sending it. The judge ignores the second after a seed while AMCL
  takes it.
- `recovery_alpha_slow` and `fast` to zero (Nav2's default) in phase 2, when
  the search takes over recovery. `tests/test_robot_model.py` asserts the
  current values and changes with them.

If AMCL does not hold 10 cm and 5 degrees at the crosses, that is a fix to
try (below), decided at phase 1's exit.

### 2. Judge: does this pose contradict the map?

**The pose it judges** is the lidar's map pose at the middle of the scan:
the latest `map -> odom` (a lookup of that one transform at time zero; a
lookup at the scan's time returns AMCL's previous belief because of the
post-dating) combined with `odom -> laser` at the scan's middle.

**The score** replaces `scan_fit`. It uses 360 beams, one per degree. Beams
that always hit the robot's own body are masked once. Beams with no return
are set aside, because the A1 drops returns on black furniture and a missing
return must never count as "through". For each remaining beam, ray-cast
across the map to the first wall and compare. To blunt the angle problem,
each beam is compared against its own prediction and its neighbours' within
±3 degrees, always in the direction that makes "through" harder to claim.

- **agree**: within tolerance of any of those predictions (20 cm plus five
  percent of range to start).
- **through**: more than twice the tolerance beyond the longest of them,
  where that prediction is within 6 m. The beam went through a mapped wall.
- **short**: anything else. Something unmapped is in the way: a person, a
  chair. Tolerated.
- **no opinion**: the ray reached unknown before any wall.

**One patch is forgiven.** The largest contiguous run of through beams is
not counted as through. It stays in the agree share as beams that fit
nothing, so a wrong pose cannot gain by it. An open door, a removed table or
a moved sofa is one patch. A wrong room sends beams through walls in several
directions.

A scan gives a **verdict** only if at least a third of its returned beams
have an opinion and the robot was not turning fast (the bridge's existing
turn gate, read from odometry). A pose **passes** a verdict scan when agree
≥ A and through ≤ T. A pose is **dropped** after N consecutive failed
verdict scans (N about four, two seconds at the 0.5 s tick).

A is set below every true pose recorded, the removed-table and
person-standing-in-front ones included. T sits above everyday life and
below the wrong room. Phase 1 measures whether any A and T separate them. If
none do, this plan stops and is rethought rather than tuned.

### 3. Search: whole map, one scan, no spin

A lidar scan already covers 360 degrees; spinning added nothing but time for
particles to collapse. The search scores one scan at every pose on every
saved map:

- **Grids.** Per map, a sharp score grid (1 at a wall, falling to 0 at
  30 cm) and a coarse one, its max over a 65 cm window. Built for every map
  on disk at bridge start.
- **Coarse pass.** Poses every 20 cm over free space and every 3 degrees,
  scored on 60 beams capped at 4 m by summing the coarse grid at the beam
  ends, **minus one for each beam whose midpoint lands on a mapped wall**.
  The end sum alone saturates: in a simulated house a true pose whose
  beams hit two unmapped chairs fell below hundreds of wrong poses that
  cross walls freely, and charging for the crossing is what tells them
  apart at this level. The window is wide enough that the true pose's
  coarse cell always scores at least as well as the true pose on the end
  sum (Hess's step rule). About 9 million lookups per map, plus as many
  for the midpoints; no ray casting here.
- **Candidates.** The sixty best distinct peaks (0.3 m or 10 degrees
  apart), each climbed on the sharp grid (a step of 5 cm or 1 degree at a
  time, while the score improves) and the tops that meet deduplicated,
  then run through the judge. (Built first as a fixed ±35 cm, ±5 degree
  window; the climb finds the same tops in fewer lookups.) The judge
  decides; the grid scores only choose what gets judged.
- **Commit.** A candidate is good enough to commit when agree ≥ A + M,
  through ≤ T, and two thirds of its returned beams have an opinion. It is
  the **clear winner** when its agree beats every other committable
  candidate's by M. The gap between A (where the judge drops) and A + M
  (where the search commits) stops the robot flapping between "found" and
  "lost". A clear winner seeds AMCL on its second clear win in a row
  (carried forward by the odometry between the two), and the state becomes
  `checking`. One win was enough at first; on the floor a scan taken while
  the robot was being carried won once at a wrong place by the piano, which
  then passed the judge at 0.58 and led the true place's 0.82 by too little
  for a rival. A carried robot moves between two searches while its wheels
  say it did not, so the two wins never agree.
- **Catching a slip or a flip.** If the search finds a committable
  candidate more than 0.3 m or 10 degrees from the held pose whose agree
  beats the held pose's by R, that candidate is a **rival**. One search is
  one scan's opinion, so a rival **stands** only when two searches in a
  row name it (within 0.3 m and 10 degrees of each other). From then on
  every verdict scan fails until a search names no rival, so the pose
  drops within N scans; if the rival is a clear winner it is seeded on the
  same tick. (Round ten had this rule; the cut-back lost the part that
  keeps failures counting, and a lone failure every five seconds never
  reaches N in a row.) That is how a
  metre's slip along a hallway or a half turn in a room is caught: the true
  pose also agrees with the end wall or the sofa, and wins. A plain corridor
  has no winner, and the robot rightly stays where odometry put it. R is
  read from recordings: above any lead seen while driving correctly, below
  the lead the true pose shows in the metre-off and wrong-heading
  recordings.
- **Which map.** The search runs on every saved map, in every state:
  floors that share outside walls can pass the judge on the wrong floor,
  and only a rival on the other map shows it. A clear winner on another
  map switches only on its second clear win in a row, so a mid-air scan
  during a carry never reloads the map. The bridge then loads that map
  (asynchronously, so the tick never blocks) and seeds it once the new map
  has arrived. The judge is paused from the load request until AMCL has
  taken the seed: a map load leaves AMCL's particles where they were,
  which on the new map is simply a wrong pose.
- **No tie-breaking tricks.** If no candidate is clear, the robot waits for
  the next scan. Any motion, a trip, a gamepad drive or a hand push, gives a
  new view that breaks the tie. There is no prior favouring the last pose:
  it is wrong after a carry.

The search runs in one thread in the nav bridge on the latest scan:
back to back while `searching`, and every five seconds while `tracking` or
`checking`. The maths is plain numpy functions in `robot_nav.py`, tested on
synthetic and recorded scans. Timed on the robot in phase 2 against two
seconds per map.

### 4. States

One state, read by everyone, replaces `localized`:

| state | meaning | caption | trips | save place |
| --- | --- | --- | --- | --- |
| `tracking` | passed N verdict scans and has rolled 0.3 m since it was seeded | none | yes | yes |
| `checking` | a pose not yet confirmed by motion; **clean** if its latest verdict passed | "checking my position, sure after a short drive" | start when clean, else held | no |
| `searching` | no pose, or the pose was dropped | "finding my place, moving me by hand helps" | held | no |
| `stale` | lidar not scanning | "lidar off" | held until the lidar is up | no |
| `off_map` | 20 s of searches with nothing passing A on any map | "can't find my place here, put me somewhere I know or start mapping below" | refused | no |

- The **motion gate** (0.3 m of wheel travel) means a robot that wakes and
  is never driven stays honestly `checking`, and a robot turned by hand in
  the air confirms nothing.
- **Leaving `stale`.** The first scan after the lidar starts puts a held
  pose into `checking` and no pose into `searching`; the pose is judged
  from there like any other.
- A **running trip** continues through `checking`. If the pose is dropped,
  the trip stops and is held, and it resumes on its own when the robot is
  clean again.
- **Barriers** come from TF's pose in every state except `off_map`. A
  doubtful pose is still the best guess, and stopping wrongly beats not
  stopping. In `off_map` only the cliff and forward sensors guard.
- **Save place** is refused outside `tracking` with "drive me a little
  first", on the dashboard and through the voice tool.
- **Dashboard.** The marker is solid in `tracking`, soft in `checking`, grey
  in `stale` (soft blue read as sure on the floor), hollow in `searching`,
  and absent in `off_map`. State word,
  shares, candidates and the Localize button live behind the debug toggle.

### 5. Trips and the voice

**Held trips** reuse the pending-goal slot the bridge already has. A trip
asked for while the robot is not ready, or stopped because the pose
dropped, sits there marked `held`, under the one serial it was given. It
leaves on its own once the robot is `tracking` or clean `checking`. It gives
up after 30 s of holding in total, ending `failed` with reason `lost`, or at
once with `off_map`. "Stop", the dashboard's Cancel button and the nav lock
cancel a held trip exactly as a running one. The gamepad cancels a running
trip, as today, but leaves a held one alone: the robot is not moving, and a
push is exactly what the caption asks for. The bridge's `navigating()`
counts a held trip, so the rail and lidar stay up.

Two things today's bridge does would break a held trip, and change:

- **Asking again.** Today every request replaces the goal under a new
  serial. A request for the place the current trip already names, held or
  running, now returns that trip under its serial; a different place
  replaces it as today.
- **The old attempt's result.** A resumed trip is a new Nav2 goal under the
  same serial, so the stopped attempt's late "cancelled" result could end
  it. The bridge tells them apart by Nav2's goal handle.

A place on
another map is held rather than refused unless the robot is `tracking`, so
a robot carried upstairs never says the bedroom is on another map while
standing in it.

**The voice** speaks in the model's own words. The tools hand it facts and
a note on what helps, never a sentence to read out. `navigate_to` already
returns at once and `check_navigation` already waits, so little changes:

- **A short wait first.** When a trip is held for the localizer,
  `navigate_to` waits up to 3 s for it to leave. If it leaves, the reply is
  the ordinary "driving" one and nothing is said about position. If not,
  it returns `held` with a note: the robot is working out where it is and
  the trip leaves on its own; say so briefly, then call `check_navigation`
  to wait.
- **Waiting.** `check_navigation` waits through a hold as it does through
  driving. When a trip gives up, its result carries the reason, `lost` or
  `off_map`, and a note on what helps: move the robot a little and ask
  again, or put it somewhere it knows.
- **What ends a trip.** Only `stop` and `cancel_navigation`, as today; a
  turn cancelled by stray speech leaves the trip alone. But today "stop"
  heard mid-turn only cancels the turn and runs no tool, which would leave
  a held trip to leave on its own a moment later. So "stop" or "cancel"
  heard while a trip is held or running also cancels the trip.

A trip held only for the lidar to start returns at once with a note that it
leaves on its own. Anything after the tools have returned (a drag after the
trip left) shows on the dashboard only. Speaking up on its own is the UX
audit's cluster A, a separate piece of work. The trip tool's description
loses "spins in place to find itself" and gains "may wait a few seconds
while it works out where it is".

### 6. Settings

Arlen: "I want to be able to turn features off, and tweak values."

Every bar and switch in this plan lives in one file,
`~/.config/robot-pet/localization.json`, built the way
`src/config/drive_tuning.py` is: a frozen dataclass of defaults, a missing
key means the default, and the dashboard's debug panel edits it. The
bridge reads it at start, so a change applies on a bridge restart.

- **Values:** the judge's tolerance, neighbour degrees and 6 m cap, A, T,
  N, M, R, the 0.3 m motion gate, the search interval while tracking, the
  30 s hold and the 20 s off-map wait.
- **Switches**, each for a part that the rest still works without: the
  forgiven patch, the neighbour rule, the rival, searching other maps,
  held trips resuming on their own, the trip tool's 3 s wait, and the
  learner (off, watch, on).

The tracker, the judge and the search themselves have no switch: without
them there is no localizer. Defaults are the values phase 1 measures, and
the replay tool reads the same file so a tweak can be tried on recordings
before the floor.

### 7. Learner: the map follows the house

Arlen: "If I move a chair or close a door or remove a coffee table, the map
gets updated and persisted. groundfloor is groundfloor." And on 2026-11-01:
"Why not make learning simply be more of a gradient acting on the map. Walls
stay black, because they're fixed, but if a chair moves, its shifted a shade
grayer (and new chair legs appear, gray, in a new location)."

**Evidence, not pixels.** Each square of a map keeps its last ten visits:
for each, whether the robot saw something there (full) or saw through it
(clear). The saved map counts as three visits to start: black squares start
with three full, white with three clear, unknown with none. A visit that
did not look at a square leaves it alone, so a corner seen once a month keeps
its evidence. Nothing fades with time; a square changes only when beams show
it has.

Ten bounds the history, so a table seen for years still goes. Walls are seen
on every visit, so their ten stay full and one bad visit cannot move them.

**What one scan says.** Only while `tracking` and clean (the verdict passed,
or the self-check vouched), not in someone's hands, and not turning (the
judge skips those scans already), from the judge's own laser pose. For each
beam that came back, within 4 m:

- the square it ended in was looked at, and was full;
- the squares it crossed, up to 0.1 m short of the end, were looked at and
  were clear.

A beam with no return teaches nothing, so black furniture and the open front
door erase nothing. A beam longer than 4 m clears only the squares within
4 m.

**One visit, one mark.** A visit's scans add up per square (looks and
hits). The visit's mark for a square is full when at least a third of its
looks ended there (a wall seen at a slant has beams grazing through it),
clear otherwise, and none if fewer than two scans looked. A door shut for
most of a visit is full for that visit. A visit closes at the first lidar
stop at least 30 minutes after the last one closed, and counts only if the
robot drove at least 1 m while learning in it: a robot parked for an hour on
a slightly wrong pose is one visit at most. Trips closer together than 30
minutes add to the same visit. An open visit lives in memory: a restart
loses it, and a map switch drops it.

**Two maps from one tally**, each with its own rule. AMCL, the judge and the
search are unchanged: they read the strict map as they read the saved map
today.

- **The strict map**, for AMCL, the judge, the search and the dashboard. A
  square is black when its visits are more often full than clear and at
  least three are full; white when it is not black and was seen on at least
  two visits; unknown otherwise. A door open half the time is white, not
  unknown: the judge's rays stop at unknown, which would hide the wall
  behind the door. It is written over the map's own .pgm in the format
  map_saver writes, so map_server, AMCL and the search read it unchanged.
- **The planning map**, for Nav2's routes. A square never seen is unknown.
  One black on the strict map and never changed (below) is black, a wall.
  Every other square seen is a grey of 50% times its share of full visits,
  even where the strict map is black, so a door shut on eight visits in ten
  is costly but never a wall. Grey only costs, it never forbids, and the
  live lidar cannot lower it; a shut door is in the way because the lidar
  sees it. 50% is a start: around 80% crosses the planner's limit for its
  shortcut attempts (cost 200) and changes how it plans near grey.

**Changed for good.** On the strict map, a door shut on most visits turns
black, and as a wall on the planning map it would forbid routes through it
even when it stands open. The ten-visit window also forgets that the door
was ever open. So each square also counts the robot's own full visits and
clear visits (not the saved map's three), each stopping at two. Once both
reach two, the square is marked changed until "forget what you learned" or
a new map under the same name, and a changed square is never a wall for
routes. The mark is permanent on purpose: a door shut for a week stays
usable the day it is opened. The cost: furniture moved onto floor the
robot had seen empty is only ever grey for routes, so a route may head
through it until the lidar sees it and plans around.

Adding a square to the strict map takes more than removing one. A new couch
on white floor (three clear) turns black on its fourth full visit; a
removed coffee table (three full) turns white on its third clear one. That
is on purpose: a wrongly added wall has the true beams pass "through" it and
fails the judge, while a wrongly removed one only has beams stop short,
which costs little. A table the robot has seen on all ten of its visits
takes five.

**Applying it.** When a visit closes, the lidar has just stopped, so no trip
is driving. The bridge builds both maps from the tally and the saved map,
writes each to a temporary file and renames it over the old one (a power cut
never leaves half a map), and asks both map servers to reload. That runs as
a background job, the way saving a map does: a reload waits for the
server's reply, and the tick must not. The pose is stale while the lidar is
off anyway, so the next scans check it afresh. Reloading the same map
leaves AMCL's particles where they were (switch_map relies on that), and
size and origin never change, so places and barriers are untouched. The
search's grid cache must notice the .pgm changing, not only the .yaml.

A floor switch loads both of the new floor's maps, and a trip stays held
until both have loaded, so the robot never finds its place upstairs while
planning on downstairs.

The tally is the truth, written the same safe way. With the learner on, the
bridge builds both maps from it on start, so a crash between two writes
heals itself. In watch or off, the bridge puts the saved map back if
learning had replaced it, and the planning map is the saved map itself; the
tally is kept, so turning learning back on loses nothing. The tally also
keeps when the last visit closed, so a restart does not start the next
visit early.

A change big enough that the robot cannot find its place is never learned,
because learning needs a sure pose. Remaking the map (saving under the same
name) is the answer.

Files, in `~/.config/robot-pet/learned/`: `<map>.npz` (each square's last
ten marks, its two counts, and when the last visit closed),
`<map>.original.pgm` (the map as saved; "forget what you learned" copies it
back and deletes the rest) and `<map>.planning.yaml` + `.pgm` (raw mode).
Saving a map under an existing name deletes its learned files.

Nav2: a second map_server (`planning_map_server`, topic `planning_map`,
like the keepout mask server) serves the planning map in localize mode. The
global costmap's static layer reads `planning_map` with `trinary_costmap`
off, so grey becomes cost. In mapping mode the launch file points the
planner's `planning_map` at slam_toolbox's `/map`, which reads the same with
trinary off. With nothing learned, the planning map is the map itself.
Barriers stay in their own keepout mask, which the costmaps add on top, so
learning never weakens a barrier.

**Switches and values.** `learner`: off, watch or on. Watch learns and
shows but applies nothing: only the tally goes to `learned/`, the
dashboard draws the changes from it, and the robot keeps using the saved
map. On applies them. Values: 4 m, 0.1 m short of each end, a third of
looks, 30 minutes, 1 m, 50% grey. Ten visits kept, the saved map counting
as three, and two visits each to mark a square changed are fixed: they
shape the tally from its first visit, so changing them later would mean
forgetting.

**The dashboard.** Drawn into the map picture the bridge already makes, in
watch mode as well as on: squares the learner has made black that the saved
map had white, squares it has made white that were black, and squares
marked changed, each in its own colour. A line: "learned from N visits,
last <time>". "Forget what you learned", per map, behind the debug toggle.

## Fixes to try

Each row is a refinement a design review proposed, mostly from a simulated
house. Each is built only when a recording or a floor test shows its
symptom. Most are a few lines.

| if a recording or test shows | try |
| --- | --- |
| A wrong pose passes because its through beams all sit in one wide arc (a corner seen from another room's corner) | Cap the width of the forgiven patch for commits, about a door's width. |
| The true pose is missing from the sixty candidates in a cluttered room | Keep every peak near the best score instead of a fixed sixty. |
| A false slip or flip is flagged while driving correctly | Refine the held pose on the sharp grid before comparing it with candidates. |
| A drag of under a metre mid-trip drives on the wrong pose for the five to ten seconds the rival takes | Once a second, refine around the held pose within ±0.5 m and ±20 degrees; a clearly better fit that passes the judge is seeded. |
| A search started before a re-seed flags the old pose's spot as better | Throw away results whose scan predates the latest seed. |
| The judge fails a fresh seed because AMCL had not taken it yet | Instead of the fixed one-second pause after a seed, wait for the first new `/amcl_pose` near the seed, ignoring the latched replay. |
| A remembered pose facing the open front door never gets a verdict and a trip never leaves | Let a trip leave after 5 s with no verdict scans, and let the trip's own motion judge it. |
| A person standing in front drops agree below A | Leave short beams out of the agree share when through is low. |
| The search misses two seconds per map | Fewer coarse beams, a 3 m cap, fewer candidates, fewer judge beams; then an FFT coarse pass; last, AMCL's own global init with 50 000 particles. |
| The tick or barrier publisher stalls while the search runs | Search less often while tracking; then move the search to a subprocess. |
| AMCL misses 10 cm or 5 degrees at the crosses | Replace AMCL's corrections with the sharp-grid refine around the odometry pose. |
| AMCL's particles collapse with the new settings | `resample_interval` 2, `sigma_hit` 0.15. |
| Scans taken while Nav2 turns are judged wrongly | De-skew scans by odometry yaw rate. |
| A carry with the lidar on slips past the judge | Keep the first scan of a still period and compare each new one against it. |
| One bad visit (a wrong pose the judge passed) changes many squares at once | Skip a visit that would change more than a set number of squares. |

## Phases

Each phase ends with something Arlen can see on the floor or in a replay.

### Phase 1: the judge (four evenings)

- **Recorder.** A `record` command in the bridge, with a label and optional
  seconds, saves the scan, the TF pose, AMCL's pose and odometry to JSON
  lines under `~/.config/robot-pet/recordings/`. A button in the debug panel
  sends it. Truth comes from three or four crosses taped on the floor and
  measured from two mapped walls; elsewhere Arlen's sentence is the label.
- **Recordings to take**, during normal floor tests: true pose at the
  crosses and places; right spot wrong heading; the counter; the rug; the
  wrong room and the metre-off pose; a drag along the hallway; a gamepad
  drive around the floor while localized; the open door and the dark sofa;
  the removed coffee table; a cluttered room; a person walking past and one
  standing in front; a carry with the lidar on.
- **Replay tool**, `scripts/localization-replay.py`: prints today's
  `scan_fit` beside the new agree and through shares for every recording,
  AMCL's error at the crosses, and the beams that always hit the body. A, T,
  the tolerance and N are read off its table, and the table goes into this
  document.
- **Map audit.** The replay draws where through beams land at true poses.
  Clusters are map errors to fix or re-map. Spread everywhere means the
  tolerance is wrong.
- **`robot_nav.beam_check(...)`**: the judge as pure numpy, tested on
  synthetic maps and on the recordings.
- **States** in the bridge: `tracking`, `checking`, `searching`, `stale`.
  `localized` becomes "tracking or clean checking". Save place needs
  `tracking`. The 0.4/0.6 flip goes.
- **Spins stay for now.** Until the search exists, relocalize still spins,
  but a spin is accepted only if `beam_check` passes *and* today's 0.6 bar
  holds. A trip whose spins all fail ends `failed (lost)`.
- **AMCL config** and the bridge carrying the seed forward.
- **Settings file** with the judge's values and switches; each later phase
  adds its own.
- **Dashboard**: marker and caption in front; the `searching` caption reads
  "lost, press Localize" in this phase only.
- **Exit, on the floor.** The wrong room is never shown as tracking and a
  spin there is never accepted. A true pose reaches `tracking` after a short
  drive. The counter drops to `searching`. A localized robot driven by
  gamepad past the door, the sofa, the removed table, the clutter and a
  person stays `tracking` the whole way. A robot carried while asleep shows
  `stale`, never a solid marker in the old room. The replay table, the map
  audit, AMCL's error at the crosses, and the verdict on the fixes-to-try
  rows these recordings can answer are written into this document.

#### Results, 2026-10-30 (ground floor)

Twenty recordings, replayed at the pose the robot believed. Agree is the
5th percentile over the recording, through the 95th.

| recording | robot really was | agree | through |
| --- | --- | --- | --- |
| crosses 1 to 3 | right | 0.93 to 0.98 | 0.00 to 0.01 |
| cross 4, by the piano | right | 0.79 | 0.06 |
| rug by the sofa | right | 0.79 | 0.06 |
| door open, facing it | right | 0.71 | 0.09 |
| dark sofa | right | 0.66 | 0.09 |
| table and chairs moved a metre | right | 0.56 | 0.04 |
| cluttered room | right | 0.55 | 0.16 |
| person walking past | right | 0.65 | 0.12 |
| person standing in front | right | 0.66 | 0.03 |
| gamepad drive, 2 minutes | right | 0.68 | 0.07 |
| on the counter | wrong | 0.42 | 0.04 |
| a metre off (cross 2 to cross 1) | wrong | 0.35 | 0.00 |
| wrong room (cross 4 to cross 1) | wrong | 0.22 | 0.02 |
| quarter turn, lifted | wrong | 0.30 | 0.00 |

- **A stays 0.55; T goes from 0.10 to 0.20.** Every right pose agreed 0.55
  or more and every wrong one 0.43 or less. At 0.10 the living room failed
  at the right pose: chair and table legs the map remembers let 8 to 16% of
  beams through, in many small patches, so the forgiven patch does not
  cover them, and the cluttered room and the person walking past were
  dropped. At 0.20 no right recording fails for more than half a second
  and no wrong one passes a single scan.
- **Through did not catch any wrong pose here; agree caught them all.** The
  case T was built for, a wrong room that a spin found and that fits well,
  was not recorded (a spin cannot be made to land wrong on demand). Phase
  2's search tests on the recordings are where it shows up.
- **T goes to 0.25 in phase 2.** Driven back into the middle of the
  living room by the table, the true pose let 24 to 26% of beams through
  the chairs the map remembers, so at 0.20 it dropped on every visit. At
  0.25 one scan in six fails there, never two in a row. At 0.30 every
  recording replays exactly as at 0.20: no wrong winner is gained.
- **Moves the robot is not told about are caught in about 6 s**: lifted a
  metre along the passage, and carried to another room with the lidar on.
- **Crosses.** The lidar's distance to every taped wall matches the tape to
  1 to 2 cm. The map draws each wall 4 to 10 cm inside its real surface
  (walls are two or three cells thick), so AMCL at the crosses is as good as
  the map can show, about 5 cm.
- **Map audit.** The kitchen is clean. Through beams cluster on the living
  room's table and chair legs and along the piano wall. Re-mapping would
  not help: chairs move every day; this is the learner's job.
- **Body beams:** none. **Scan time:** 0.133 s; `--stamp-offset` not
  needed while the right poses agree this well.
- **Fixes to try:** none of the rows these recordings can answer is needed
  (a person standing in front, AMCL at the crosses, a carry with the lidar
  on, a wrong pose passing through one wide arc).
- **Floor checks, same evening.** Lidar off, then carried from the living
  room to the kitchen: `stale`, but the soft blue marker read as sure, so
  `stale` is now grey. Lidar back on: `searching` within seconds. Localize
  there found the kitchen.

### Phase 2: the search (four evenings)

- `robot_nav.global_search(...)`: grids, coarse pass, sixty candidates,
  refine, judge, commit and clear-winner rules. Timed on the robot.
- The search thread; the slip check while tracking; the map switch.
- Localize becomes "search now" and moves behind the debug toggle. The
  spins, `scan_fit` and its bars are deleted. The `searching` caption
  becomes "finding my place, moving me by hand helps".
- AMCL's recovery alphas to zero.
- Tests on recordings: every true pose is among the sixty and wins; the
  wrong room and the counter produce no committable candidate; the
  metre-off and wrong-heading recordings produce the true pose as a better
  candidate, which sets R's ceiling; the gamepad drive never does, which
  sets R's floor.
- **Exit, on the floor.** Carried to twenty spots and set down with nothing
  pressed: the right pose within about 5 s where the spot is unambiguous,
  `tracking` within the first 0.3 m of a trip, never a wrong room. Slipped a
  metre along the hallway, or turned half round by hand: dropped and
  re-found within a few seconds. Ten minutes of gamepad driving: never
  dropped. Carried upstairs with the second floor saved: the map switches
  and a trip there runs.

#### Recordings, 2026-10-30 (ground floor)

The phase 1 recordings replayed through the search every fourth scan
(`scripts/localization-replay.py --search`), scans taken mid-turn skipped.

- **Never a wrong room.** At the crosses, the rug, the open door and the
  dark sofa the clear winner was the true pose on 18 to 20 scans of 20.
  One wrong winner in the whole set: cross 4, 20 degrees off, where the
  piano wall blurs the heading. The counter produced no committable
  candidate at all.
- **Moves are found.** Lifted a metre along the passage, and carried to
  another room: a clear winner away from AMCL's pose on 36 of 38 and 45 of
  57 scans. The gamepad drive: 83 winners of 98 scans, all at AMCL's pose.
- **Clutter is where it waits.** With the table and chairs moved a metre
  the true pose won 1 scan of 19; in the cluttered room 2 of 20; with a
  person standing in front 10 of 19. There the robot waits for the next
  view, as designed: a short drive or a push breaks it. These are the
  spots to watch on the floor.
- **R is 0.4.** While driving correctly a rival led the held pose by 0.34
  at most: AMCL's heading lagging 10 to 15 degrees for a few seconds after
  a turn. A slipped or carried pose was led by 0.62 or more (lifted a
  metre, median 0.71; turned and lifted, 0.67 at least; carried from
  cross 2, 0.62). Carried to another room was led by only 0.36 at the
  median, but the judge drops that pose by itself.
- **Time:** 0.24 s per search on the Mac at the median, 1.21 s at most.

### Phase 2b: the self-check and handling (built 2026-10-31)

Found on the floor: reversing into the spot by the living room table, the
map disagreed (the table and chairs had moved since mapping) and the judge
dropped a pose the wheels plainly vouched for. Two additions, each with its
own switch in localization.json.

- **The self-check** (`robot_nav.scan_check`). A scan that fails the map is
  compared with the last scan that passed, carried by the wheel odometry.
  The map plays no part, so the tolerance is tight: 0.08 m + 2% of range,
  neighbours 2 degrees each side. Matching 0.75 of the beams or more, the
  verdict does not count. A rival still fails every verdict.
- **Handling** (`update_handling` in the bridge). While not driving, both
  cliff sensors past their cliff trip means picked up; rocking more than
  0.5 degrees within a second means pushed. While driving (a non-zero
  `/cmd_vel`, `/nav_lock`, or 1.5 s after either), the cliff sensors stay
  cliff sensors and, with `lift_tip` on, a tip of 6 degrees from how the
  robot sat means picked up. `lift_tip` is off by default: a quick gamepad
  stop read past 6 degrees on the floor, and a pick-up while driving is rare. A cliff met while driving is not a pick-up until the floor is back
  under both. Handled, the pose drops to checking and the judge and the
  search wait; put down (floor under both, no rocking for 1 s) the pose is
  checked afresh and a search runs at once. Put back where it was, the
  judge re-earns the pose with a short drive; carried elsewhere, it fails
  and is found.

#### Recordings, 2026-10-31

Self-check score (share of beams matching the last good scan):

| Case | Score |
| --- | --- |
| Reverse into the table spot (true pose, map disagrees) | 0.88 or more |
| Person walking past | 0.79 at least |
| Hidden 20 cm slide | 0.20 to 0.68 |
| Hidden 10 degree turn | 0.29 to 0.74 |
| Gamepad drive, scans the map fails | 0.48 to 0.69 (not excused, as before) |
| Carried to another room | 0.13 to 0.46 |
| Lifted along the passage | about 0.1 |

IMU and cliff sensors:

| Case | Reading |
| --- | --- |
| Still on the floor | rocking 0.17 degrees at most |
| Pushed by hand | rocking 1 to 2 degrees |
| Driving, rug edge, sill | tip 3.1 degrees, rocking 3.8 at most |
| Quick gamepad stop | tip past 6 degrees |
| Picked up | tip 11 degrees |
| Cliff sensors on the floor | 76 to 92 mm |
| Cliff sensors when lifted | past 110 mm within 0.1 to 0.2 s |

### Phase 3: held trips, voice, off map (built 2026-10-31)

As built: a trip is `held` (with `waiting_for` lidar, position or nav2) or
`navigating`. Instead of a serial and Nav2's goal handle, each goal sent to
Nav2 counts as an attempt, and a callback for an earlier attempt is ignored.
A held trip that resumes is a new attempt. The trip remembers its map. The
voice's wait (`trip_wait_s`) comes back in the `navigate_to` reply, so the
voice reads no settings file. Besides a pose drop and a pick-up, the lidar
going quiet mid-trip, a new pose (search, dashboard pose or Localize button)
and a map switch also hold a trip; time spent switching maps counts toward
`hold_s`. A place on another map is refused only when the robot is
`localized()` (tracking, or checking with the latest scan passing), the same
rule a held trip is failed by.
"Stop" or "cancel" in the first three words heard cancels the trip, whether
or not a turn starts. Off the map the barriers stand down, as there is no
pose to measure them from.


- Held trips in the bridge, the trip tool's short wait and notes, `off_map`,
  the held goal drawn hollow on the dashboard map with Cancel live. The
  dashboard's `nav.js` checks the string `'navigating'` in three places;
  each learns `'held'`.
- The lidar on the wake word needs the lidar power plan, which is not
  written yet. Until then the first trip after an idle waits silently for
  the lidar.
- **Exit, on the floor.** "Go to the kitchen" from idle: it leaves with no
  wiggle. Dragged mid-trip: it stops, re-finds itself and resumes, with the
  marker and caption telling the story. Dragged while awake, then asked for
  a trip: it says it is working out where it is, then leaves. Found within
  3 s: it just goes, saying nothing about position. "Stop" during the hold:
  the trip is gone. On the counter: it says it is working it out, then at
  20 s that it can't place itself here, once.
  Coffee table removed: trips still run.

### Phase 4: the learner (about four evenings)

Three steps, each ending with something to look at. It watches for a week
before it touches the map.

- **4a. The tally, on recordings.** `src/map_learner.py`: a few flat
  functions (what one scan says, closing a visit, the strict map, the
  planning map) with unit tests. Recordings gain `clean` and `handled` for
  each scan, so the replay learns by the bridge's rules. The replay tool
  gets `--learn`, which takes each recording as one visit (a recording may
  be replayed as several) and writes both maps as PNGs. Recordings, by
  pressing Record during ordinary trips: three or four with the coffee
  table in place, three or four on separate occasions with it moved, and
  two each with a door shut and open.
  **Exit, on the replay:**
  - The coffee table is gone from the strict map after three visits, and
    after five once table-in visits have filled all ten; grey on the
    planning map meanwhile.
  - The door, seen shut twice and open twice and then shut on ten more
    visits, is marked changed and still grey on the planning map.
  - An open-door recording, judged against a learned map with that door
    black, still passes.
  - No wall loses a square, open floor gains no speckle, and unknown stays
    unknown.
  - Against the learned strict map, every recording's known right poses
    still pass, its known wrong poses still fail, and the search finds the
    robot as well as before. (An old recording made with the table still
    there may agree a little less once the table is learned away; that is
    fine.)
  - Recordings made after a change agree at least as well against the
    learned strict map as against the saved one, with no more through
    beams.
  - One visit replayed four times with its poses shifted 10 cm and 5
    degrees takes no square from a wall.
  - A unit test: two bad clear visits on a wall seen ten times leave it
    black on the strict map.
- **4b. Watching, in the bridge.** The bridge learns while tracking, closes
  visits and writes the tally; the dashboard draws the changes, the visit
  count and "forget". `learner: watch`. The tick time and the Pi's CPU are
  measured before and after.
  **Exit:** a week of ordinary use; the drawn changes are the house's
  changes and nothing else.
- **4c. Applying.** Both maps written and reloaded in a background job, the
  search's cache keyed on the .pgm, the planning map server, and the static
  layer reading it. First, on the floor: a same-map reload keeps AMCL's
  pose, the Nav2 1.5 parameter names for grey are what this plan says, and
  the mapping-mode pointer reaches the planner's costmap.
  **Exit, on the floor:** remove the coffee table; after three visits on
  separate occasions (five, once it has filled its ten) it is gone from the
  map, and trips route through where it stood. A door shut on some visits:
  trips still use it when it is open and go around when it is shut. A
  floor switch with learned maps on both floors: the trip after it plans on
  the new floor's planning map.
  "Forget" brings back the saved map. Places, barriers and accuracy are
  unchanged.

To watch for:

- **Mirrors and glass.** A mirror's beams look as if they pass through the
  wall behind it, so the learner would clear that wall, and the live lidar
  cannot see a mirror either. If the watch week shows one, a barrier line
  in front of it holds (barriers are never learned).
- **A repeated error**, such as a lidar mounted slightly crooked, repeats on
  every visit, and separate visits do not guard against it. The drawn
  changes are how it would show; "forget" is the way back.
- **Changed marks never clear.** The watch week counts them. If wall edges
  light up, two visits each becomes three. Two route symptoms, with
  opposite fixes:
  - The planner steers around grey squares it could cross: lower the 50%.
  - Routes keep heading through furniture that has settled in a new spot,
    then turning back: clear the mark after ten visits that agree, at the
    price of a door shut for that long becoming a wall again.
- **The Pi.** Casting the scan's rays twice a second within 4 m is about
  what the judge already does; 4b measures it.

### Driving out of tight spots (switched 2026-10-31)

Found 2026-10-31: with a table leg by its tail and open floor ahead, a trip
whose target was behind it gave up. The controller (Regulated Pure Pursuit)
turns in place first and the planner (NavFn) plans for a point, and the stuck
moves are only spin, back up and wait: none pulls forward. The same day an
open door stopped a trip: the planner kept routing the point past it and the
controller kept refusing every route as a collision.

So both were swapped the same day: the Smac Lattice planner, which plans the
real footprint, and the MPPI controller, which scores about a thousand short
moves against it each tick. The cost is CPU on the Pi 5 (already near half
busy at idle), so MPPI runs trimmed: 1000 tries of 3 s at 10 Hz. If the Pi
can't keep up, trim further, or go back and add a short drive forward as the
first stuck move instead.

## Alternatives rejected

- **Moving the bars, or reading AMCL's covariance.** The wrong room scored
  inside the true range; a tight particle cloud sat in the wrong room.
- **AMCL's own "lost" signals.** Nav2 never publishes its fast/slow ratio,
  and the floor already showed a confident cloud in the wrong place.
- **slam_toolbox localization, Cartographer, or prism_loc as-is.** No global
  search, not packaged for Lyrical, or Humble-only and months old.
- **Spinning smarter.** Spinning works around a search that isn't one.
- **A prior favouring the last known pose.** Wrong after every carry.
- **A separate localizer process.** One thread in the bridge is enough
  unless the tick stalls.
- **A one-turn nudge to break ties** (cut by Arlen), **a gamepad speed cap
  while unsure** (cut by Arlen), **lift or carry detection** (cut by Arlen).

## Decisions

Arlen's:

- No carry or drag detection; "do I know where I am, is there another point
  that makes more sense" covers everything.
- Everyday changes never cost the pose; one patch through is "something
  changed", as long as the rule is common and not janky.
- Five states.
- The robot never moves itself to localize.
- A stopped trip resumes on its own.
- The lidar starts on the wake word; only the lidar.
- The gamepad is never slowed or blocked.
- The Localize button lives behind the debug toggle once the search runs
  on its own.
- One map changed in place by the learner, with "forget what you learned";
  three visits, 30 minutes apart.
- On 2026-11-01: learning is a gradient. Walls stay black; furniture that
  moves goes grey on the map routes are planned on. A square seen both full
  and empty is marked changed for good, until "forget": a door seen open
  stays usable for routes however long it is then shut.
- Recordings come from normal floor tests, not a separate evening.
- On 2026-10-28, after ten review rounds had grown the plan fourfold: cut
  back to the skeleton, with the refinements kept as a list of fixes to
  try.
- On 2026-10-30: restore round ten's standing rival, which the cut-back
  broke. Every value and optional part is configurable. A nearby check every
  second (catching a half-metre drag in one second instead of five to ten)
  is on hold, listed under fixes to try. Restored from round ten too: the
  coarse pass's wall-crossing charge, two clear wins and a paused judge for
  a map switch, held trips safe from stray words, a re-ask, a gamepad push
  and the old attempt's late result, and what leaving `stale` does.
  Multi-floor is needed. The learner is wanted; if phase 3 is stable, it
  may wait.

Claude's:

- AMCL stays the tracker until the crosses say otherwise.
- One drop rule: N failed verdict scans, where a standing rival (two
  searches in a row naming the same better pose) fails every scan until a
  search names none.
- One search for finding, map choice and slip checks; the judge decides.
- Phase 1 keeps the spins, judged strictly; phase 2 deletes them.
- The learner keeps each square's last ten visits, not a lifetime count, so
  a long-standing table still goes. One tally makes two maps with their own
  rules: a strict black-and-white one for finding the robot's place (the
  majority of visits), and a grey one for planning, where a changed square
  is never a wall. It runs in watch mode for a week before it is switched
  on.
- The planning map has its own map server rather than going into the
  barrier mask: the planner reads its walls from that map, and only a map
  of its own can soften a wall that the strict map keeps for finding the
  robot's place.

## Sources

- [Olson 2009, Real-time correlative scan matching](https://april.eecs.umich.edu/pdfs/olson2009icra.pdf)
- [Hess et al. 2016, Real-Time Loop Closure in 2D LIDAR SLAM](https://research.google/pubs/pub45466)
- [prism_loc](https://github.com/kjungmo/prism_loc), [parameters](https://github.com/kjungmo/prism_loc/blob/main/PARAMS.md)
- [Nav2 AMCL configuration](https://docs.nav2.org/jazzy/configuration_and_development/configuration_guide/others/configuring_amcl/); Nav2 `nav2_amcl/src/amcl_node.cpp` (seed handling, transform post-dating)
- [Beluga AMCL](https://github.com/Ekumen-OS/beluga), [slam_toolbox](https://github.com/SteveMacenski/slam_toolbox)
- Thrun, Burgard, Fox, Probabilistic Robotics, ch. 6.3 to 6.4 and 8.3.5
- [Dreame multi-floor guide](https://www.dreametech.com/blogs/blog/multi-floor-mapping-robot-vacuum)
- [Walcott-Bryant et al., Dynamic Pose Graph SLAM](https://people.csail.mit.edu/kaess/pub/WalcottBryant12iros.pdf)
