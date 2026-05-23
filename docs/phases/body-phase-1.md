# Body Phase 1 — Audio / Video Body

## Status

Complete.

## Goal

Add the physical audio/video hardware needed for conversation and early perception experiments while keeping the robot manually driven.

This phase is about installing and proving the microphone, speaker, and camera as hardware devices. It does not prove the cloud conversation loop; that belongs to Personality Phase 1.

## What Exists

- Seeed ReSpeaker Flex XVF3800 for 6-channel capture and playback through the same USB path.
- Pi Camera Module 3 Wide via `robot-camera.service`.
- Gamepad teleop and RoboClaw motion unchanged alongside audio/video services.

## Entry Criteria

- Body Phase 0 is complete.
- Gamepad teleop remains the known-good way to move the robot.
- The Pi is reachable over the network for development and logs.

## Exit Criteria

This phase is done when:

- The microphone is visible to the Pi and can capture usable test audio.
- The speaker can play local test audio at an acceptable volume.
- The camera can capture still frames or video from the robot's point of view.
- The audio/video hardware can coexist with the existing gamepad and RoboClaw setup.
- Any wake-word capability from the microphone hardware is understood enough to decide whether to use it.

## Key Decisions

- Use the ReSpeaker Flex for mic and speaker; route assistant playback through the ReSpeaker output path for echo cancellation.
- Use openWakeWord on the Pi for wake word in Personality Phase 1. Do not adopt a smart-home stack or ReSpeaker firmware wake features for v1.
- Use the Pi Camera Module 3 Wide as the onboard camera.

## Default Direction

- Keep movement manual during this body phase.

## Cross-Track Dependencies

- Personality Phase 1 needs this phase for real voice I/O.
- Personality Phase 2 can use Body Phase 0 motion for tiny bounded movement tools, but this body phase does not add autonomy.

## Not In Scope

- Distance sensors.
- IMU.
- Lidar.
- Obstacle avoidance.
- SLAM.
- Autonomous navigation.
- Cloud STT / TTS integration.
- Cloud LLM conversation.

## Notes

Hardware is bought and installed on demand. Do not install the full autonomy sensor suite here just because later phases need it.
