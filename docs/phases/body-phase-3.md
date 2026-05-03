# Body Phase 3 — Perception Offload

## Goal

Use the onboard camera and networked MacBook to add visual perception without forcing the Pi to do heavyweight inference.

The robot should be able to send frames or video snippets over the LAN, receive perception results back, and keep local safety behavior independent of the network.

## Entry Criteria

- Body Phase 1 camera is working.
- The MacBook can stay online during robot sessions.
- The Pi and MacBook have reliable enough LAN connectivity for development.

## Exit Criteria

This phase is done when:

- The Pi can publish or stream camera frames to the MacBook.
- The MacBook can run at least one useful perception task.
- Detection or scene results can return to the Pi over a clean API.
- The robot degrades gracefully when the MacBook is unavailable.
- Local body control and safety do not depend on perception offload being online.

## Default Direction

- Treat the MacBook as a perception microservice.
- Keep the network boundary clean so a future Jetson or other onboard accelerator could replace it.
- Start with object detection or scene description before trying to make perception drive navigation.

## Cross-Track Dependencies

- Personality phases can use perception results as context once this exists.
- Personality Phase 6 should treat perception as optional context, not a safety-critical input.
- Body Phase 5 may eventually use perception alongside navigation, but it should not depend on vision alone.

## Not In Scope

- Full semantic home understanding.
- Face recognition as a required capability.
- SLAM.
- Navigation.
- Docking.

## Notes

WiFi will drop. The software should treat "MacBook unavailable" as normal runtime state, not as a crash.
