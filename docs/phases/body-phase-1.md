# Body Phase 1 — Audio / Video Body

## Goal

Add the physical audio/video hardware needed for conversation and early perception experiments while keeping the robot manually driven.

This phase is about installing and proving the microphone, speaker, and camera as hardware devices. It does not prove the cloud conversation loop; that belongs to Personality Phase 1.

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

## Default Direction

- Use the ordered ReSpeaker Flex if it exposes wake/listen capabilities at a low driver or device-software level.
- Avoid adopting a large smart-home stack just to get wake word.
- Use the Pi Camera Module 3 Wide as the onboard camera.
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
