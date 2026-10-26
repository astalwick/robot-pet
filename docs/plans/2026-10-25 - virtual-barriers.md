# Virtual barriers (2026-10-25)

The ground floor map has the dining room two steps down from the living
room. On the map it is open floor; only the cliff sensors would stop the robot.
Arlen wants to draw lines the planner treats as walls.

Follows [2026-10-25 - lidar-safety-and-nav2.md](2026-10-25%20-%20lidar-safety-and-nav2.md).

## What gets built

- **Barriers are line segments in map metres, per map**, in
  `~/.config/robot-pet/barriers.json`:
  `{"groundfloor": [{"x1": .., "y1": .., "x2": .., "y2": ..}, ...]}`.
  Like places, only the nav bridge writes the file, and a barrier can only be
  drawn in localize mode (it belongs to the map in use).
- **Nav2's keepout filter** blocks them. Nav2 reads a *mask*: an occupancy
  grid in the map frame where black cells are forbidden. `robot_nav.
  render_keepout_mask(barriers, stem)` draws every segment as a 0.6 m wide
  black band (0.3 m each side: the tail sweeps 0.31 m when the robot turns in
  place, and keepout cells are not inflated) on a white grid at 5 cm, sized
  to the barriers' bounding box, and writes `<stem>.pgm` + `<stem>.yaml`
  under `~/.config/robot-pet/keepout/<map>`. No barriers: a 1x1 free mask.
- `config/nav.launch.py` starts, in **both modes**, a second `map_server`
  named `filter_mask_server` on that mask, `costmap_filter_info_server`
  (type 0 = keepout), and `lifecycle_manager_keepout` (autostart). Mapping
  mode, or a map without a mask yet, uses `config/keepout-empty.yaml`: the
  filter plugin otherwise warns every 2 s that no mask arrived. `nav2.yaml`
  adds `filters: ["keepout_filter"]` to both costmaps.
- **Changes apply live.** After a barrier is added or deleted the bridge
  re-renders the mask and calls `/filter_mask_server/load_map`, the same
  service `relocalize` uses to swap maps. The mask is also rendered before
  the bridge restarts robot-nav into localize mode (so the launch finds it),
  at bridge start, and when a relocalize converges on another map.
- **Socket verbs**: `create_barrier {x1, y1, x2, y2}`, `delete_barrier
  {index}`, and `delete_place {name}` (asked for earlier; the same erase
  gesture covers both). `nav.barriers` rides the hub message like `places`.
- **Dashboard**: barriers drawn as red lines with a translucent band of the
  real width over the current map. Two toggles on the map: *Draw barrier*
  (click two points) and *Erase* (click a barrier or a place). Both are
  sticky until toggled off; a plain click still drives there.
- Voice: unchanged. Nav2 fails a goal inside a band on its own.

## Review against how the robot is set up

- nav.json / places.json pattern kept: one JSON file per concern under
  `~/.config/robot-pet`, the bridge the only writer, atomic `_write_json`.
  The rendered mask is derived state, so it sits in its own `keepout/`
  directory and not in `maps/` (where `saved_maps` lists every `.yaml`).
- The launch file keeps reading only files at start; the exists-check for the
  mask mirrors the one for the map yaml. A redeploy that restarts robot-nav
  before the bridge wrote a mask falls back to the empty one, and the bridge's
  next `load_map` fixes it.
- Own lifecycle manager rather than growing `lifecycle_manager_localization`:
  the filter servers run in mapping mode too, where that manager does not
  exist, and `lifecycle_manager_navigation` is paused between trips, when a
  paused `map_server` ignores `load_map`.
- Mask geometry is its own, not the map's: the keepout filter converts world
  coordinates through the mask's own origin and resolution, so the bridge
  never has to parse the map yaml (no PyYAML in the Mac tests) or wait for
  `/map`.
- Tests: pure functions in `robot_nav` (round trip, mask geometry and band),
  text match on `nav2.yaml`, the dashboard whitelist, `nav_update`.
- Not done: a robot already standing inside a band cannot plan out (the
  controller sees lethal cells under its footprint). Drive it out by gamepad.

## Second layer: the gamepad stops at barriers too

Barriers only existed inside Nav2; the gamepad goes `/cmd_vel_teleop` ->
twist_mux -> robot-motion and never meets the costmap. Arlen: "if we are in
localize, and if we know we're coming up to a barrier, we should prevent
crossing the barrier."

- **The bridge knows the pose and the barriers, so it does the geometry.**
  `robot_nav.barrier_clearances(pose, barriers)` is the barrier twin of
  `scan_clearances`: each segment goes into the robot frame, is clipped to the
  body's width, and the nearest point ahead of the front edge / behind the
  rear edge becomes the forward / rear clearance (inf when none). Ten times a
  second, while in localize mode and localized, the bridge publishes the pair
  as a `Float32MultiArray` on `/barrier_clearance`.
- **robot-motion gates on it like the lidar.** `motion_node.py` subscribes
  and hands `Clearances` to the runner; `evaluate_safety` blocks forward below
  `BARRIER_STOP_METERS` (`barrier_ahead`) and reverse (`barrier_behind`),
  turning in place stays free. The stop distance is 0.2 m, inside the 0.3 m
  keepout band: Nav2 keeps the whole footprint out of the band, so its own
  trips never trip the gate, while the gamepad stops with the front edge
  20 cm short of the line (AMCL is good to 10-20 cm; the cliff ToFs stay
  the first layer).
- **No pose, no rule.** The bridge publishes nothing in mapping mode or before
  localization, and a clearance older than `BARRIER_STALE_SECONDS` (1 s) is
  ignored, the same shape as the lidar's staleness rule. Nothing to switch
  off, nothing to configure.
- Review: a new ROS topic rather than a hub message (10 Hz would be noise for
  every hub subscriber, and the motion node already takes `/scan` this way);
  the geometry is a pure function in `robot_nav` next to `distance_to_segment`
  with numpy-free math so the Mac tests cover it; the second copy of the
  lidar block in `evaluate_safety` is a copy on purpose (two uses, not three).
