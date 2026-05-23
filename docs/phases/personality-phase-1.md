# Personality Phase 1 — Hear and Speak

## Status

In progress. The core STT -> LLM -> TTS loop is implemented and demoable. Wake word activation is still open.

## Goal

Create the first real conversational loop: the robot hears speech, sends it through STT, gets a low-latency LLM response, and speaks the response back through TTS.

This phase proves that the robot can be addressed as a being, even before it can do much.

## Entry Criteria

- Body Phase 1 is complete.
- The microphone can capture audio and the speaker can play local test audio.
- The Pi can reach the internet for cloud APIs.
- API keys for the chosen speech and LLM providers are available.

## Exit Criteria

This phase is done when:

- The robot can capture spoken input.
- Speech can be transcribed.
- A cloud LLM can produce a response.
- TTS can play the response through the robot speaker.
- Wake word starts an active listening session (with a short local chime).
- After idle or explicit stop, the robot returns to wake-word-only mode.
- The interaction is reliable enough to demo repeatedly.

## Default Direction

- Run wake word detection locally on the Raspberry Pi, likely with openWakeWord.
- Play a short local chime immediately after wake word detection so the user knows the robot is listening.
- Use ElevenLabs Scribe for realtime STT and ElevenLabs Flash for TTS, streamed from the Pi over the network.
- Use a low-latency OpenAI model for the conversation loop.
- Use the ReSpeaker for audio input and output on the same USB path so built-in echo cancellation has the best chance to work.
- Avoid pulling in a large smart-home platform just to get wake word.

Until wake word lands, the web dashboard Listen toggle (`voice.json` `enabled`) is the stand-in for starting a session.

## What Exists

- `robot-voice.service` owns capture, Scribe streaming, OpenAI responses, Flash playback, conversation history, and barge-in.
- ReSpeaker driver with configurable processed mic channel.
- Dashboard voice controls and telemetry timeline.

## Still Open

- Wake word detection (openWakeWord on the Pi).
- Wake chime after detection.
- Idle timeout that ends the active session and returns to wake-word-only listening.
- Demo reliability tuning as needed.

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

The Raspberry Pi owns the active conversation state. Keep Pi services as normal host processes managed by systemd, not Docker containers, unless a specific dependency forces a different choice.

On the Pi:

- `robot-voice.service` is the conversation orchestrator today: ReSpeaker I/O, Scribe, OpenAI, Flash playback, barge-in, and session lifecycle.
- Wake word will likely be a separate always-on listener (or a clear idle mode inside `robot-voice`) that hands off to the active voice session after detection and chime.
- Existing robot services remain separate; this phase does not merge voice with motion control.

In the cloud:

- ElevenLabs Scribe for streaming STT (partial and committed transcripts).
- ElevenLabs Flash for streaming TTS playback.
- OpenAI for the LLM response (and the small `switch_voice` tool in v1).

Communication shape:

- Pi -> ElevenLabs Scribe: streaming microphone audio.
- Scribe -> Pi: partial and committed transcript events.
- Pi -> OpenAI: conversation input, streamed response tokens (and tool calls) back.
- Pi -> ElevenLabs Flash: speakable text chunks; streamed PCM back for playback on the ReSpeaker.

The Pi should be able to cancel the active STT or TTS work when barge-in or idle shutdown happens. Prefer streaming websocket sessions where the provider supports them.

## Idle and Barge-In

- Idle detection is part of the phase: after wake, the robot should eventually shut down active listening and return to wake-word-only mode.
- The exact timeout values can be tuned during testing.
- Barge-in is implemented in v1: if the robot is speaking and STT hears a new user utterance, stop playback, clear queued speech, and treat the new utterance as the next turn.
- ReSpeaker echo cancellation may be enough for v1 because input and output both flow through the ReSpeaker path. Test this before adding more software echo cancellation.
- If the robot's own speech repeatedly triggers STT, add a small amount of filtering before making the barge-in logic more complex.

## Cross-Track Dependencies

- Needs Body Phase 1 for onboard mic and speaker.
- Uses Body Phase 1 hardware; owns the STT -> LLM -> TTS behavior.
- Does not need autonomous movement.
- Does not need tool calling yet.

## Not In Scope

- Tool calling beyond the voice-switch proof of concept.
- Persistent memory.
- Emotional state.
- Autonomous behavior.
- Display expressions.

## Notes

This phase can be followed by either simple tool calling or a personality card. The important thing is proving the speech loop first.

See [voice assistant / ReSpeaker plan](../plans/2026-05-16%20-%20voice-assistant-respeaker.md) for implementation detail on the current stack.
