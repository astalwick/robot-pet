# Personality Phase 1 — Hear and Speak

## Goal

Create the first real conversational loop: the robot hears speech, sends it through STT, gets a cloud LLM response, and speaks the response back through TTS.

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

- Use cloud LLM services for the conversation loop.
- Use cloud STT/TTS if that gets the loop working quickly.
- Prefer local wake/listen if the microphone hardware exposes it cleanly.
- Avoid pulling in a large smart-home platform just to get wake word.

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
