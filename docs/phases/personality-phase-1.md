# Personality Phase 1 — Hear and Speak

## Goal

Create the first real conversational loop: the robot hears speech, sends it through STT, gets a low-latency LLM response, and speaks the response back through TTS.

This phase proves that the robot can be addressed as a being, even before it can do much.

## Entry Criteria

- Body Phase 1 audio hardware is installed or available for testing.
- The microphone can capture audio and the speaker can play local test audio.
- The Pi can reach the internet or a service host that can reach cloud APIs.
- API keys and basic cloud service choices are available.

## Exit Criteria

This phase is done when:

- The robot can capture spoken input.
- Speech can be transcribed.
- A cloud LLM can produce a response.
- TTS can play the response through the robot speaker.
- The interaction is reliable enough to demo repeatedly.

## Default Direction

- Run wake word detection locally on the Raspberry Pi, likely with openWakeWord.
- Play a short local chime immediately after wake word detection so the user knows the robot is listening.
- Stream active speech to Kyutai STT hosted on the MacBook M3.
- Use a low-latency cloud LLM for the conversation loop.
- Host Kyutai TTS 1.6B on the MacBook M3 and stream generated speech back to the Pi.
- Keep cloud STT/TTS as fallbacks if Mac-hosted speech services block progress.
- Use the ReSpeaker 4-mic array for audio input and audio output, with the USB speakers wired through the ReSpeaker path so its built-in echo cancellation has the best chance to work.
- Avoid pulling in a large smart-home platform just to get wake word.

## V1 Interaction Loop

Keep the first version simple:

1. Wait for wake word.
2. Play a short wake chime.
3. Listen for the user's utterance.
4. Send speech to STT.
5. Send text to the LLM.
6. Feed the first speakable response chunks to TTS.
7. Stream generated audio back to the Pi speaker.
8. Return to listening for a follow-up.
9. Return to wake-word-only mode after an idle timeout or explicit stop.

The first version should prove the loop, not over-specify the final conversation engine.

## V1 Software Architecture

The Raspberry Pi should own the active conversation state. Keep the Pi services as normal host processes managed by systemd, not Docker containers, unless a specific dependency forces a different choice. This keeps USB audio, playback, journald logs, and future ROS2 migration simpler.

On the Pi:

- `wakeword-service` listens for the wake word while the robot is idle.
- `voice-client` owns the active voice session: chime, recording, STT streaming, LLM streaming, TTS requests, playback, idle detection, and barge-in handling.
- Existing robot services remain separate; this phase does not need to merge voice with motion control.

On the MacBook:

- Run Kyutai STT in Docker, or use the best-supported local serving path for Apple silicon if Docker is not practical.
- Run Kyutai TTS in Docker.
- Put thin local APIs in front of Kyutai if the native serving interfaces are awkward.
- Treat the Mac as replaceable speech services: audio stream in, transcript/events out; text in, audio stream out.

In the cloud:

- Use a low-latency OpenAI model for the LLM response.
- Keep cloud STT/TTS available only as fallbacks if Mac-hosted speech services are not working yet.

Communication shape:

- Pi -> Mac STT: streaming microphone audio.
- Mac STT -> Pi: transcript updates and end-of-speech events.
- Pi -> cloud LLM: transcribed text, with streamed response tokens back.
- Pi -> Mac TTS: WebSocket or HTTP streaming request with speakable text chunks.
- Mac TTS -> Pi: streamed audio chunks for immediate playback.

For v1, prefer WebSocket-style Pi-to-Mac speech sessions because they can carry streaming inputs, streaming outputs, completion, and cancellation on one connection. The Pi should be able to cancel the active STT or TTS session when barge-in or idle shutdown happens.

The Mac hosts the speech microservices, but the Pi remains the conversation orchestrator. The Pi owns wake/idle state, LLM calls, response streaming, future tool call validation, and robot-local execution decisions.

## Idle and Barge-In

- Idle detection is part of the phase: after wake, the robot should eventually shut down active listening and return to wake-word-only mode.
- The exact timeout values can be tuned during testing.
- Barge-in can be simple in v1: if the robot is speaking and STT hears a new user utterance, stop playback, clear queued speech, and treat the new utterance as the next turn.
- ReSpeaker echo cancellation may be enough for v1 because input and output both flow through the ReSpeaker path. Test this before adding more software echo cancellation.
- If the robot's own speech repeatedly triggers STT, add a small amount of filtering before making the barge-in logic more complex.

## Cross-Track Dependencies

- Needs Body Phase 1 for onboard mic and speaker.
- Uses Body Phase 1 hardware; owns the STT -> LLM -> TTS behavior.
- Does not need autonomous movement.
- Does not need tool calling yet.

## Not In Scope

- Tool calling.
- Persistent memory.
- Emotional state.
- Autonomous behavior.
- Display expressions.

## Notes

This phase can be followed by either simple tool calling or a personality card. The important thing is proving the speech loop first.

