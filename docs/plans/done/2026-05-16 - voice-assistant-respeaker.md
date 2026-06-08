# Voice Assistant / ReSpeaker Integration Plan

Goal: bring the ElevenLabs/OpenAI realtime voice assistant proof of concept into `robo-pet` as a robot service, using the USB Seeed ReSpeaker XVF3800 for both speech input and robot speech output.

This plan is written for handoff. Basic ReSpeaker capture/playback has already been validated on the robot: the mic records, channel index `1` is the right starting channel, and speaker playback works. Treat lower-level ALSA/PortAudio details and tuning notes as implementation details to re-check if the USB device numbering or robot body changes.

## Architecture Direction

- Add a new `robot-voice` service. Do not fold voice into `robot-brain`, `robot-web-dashboard`, `robot-camera`, or `robot-vision`.
- Keep the service running under systemd. The dashboard should control listening by writing config, not by starting and stopping the unit.
- Preserve the useful tested logic from `elevenlabs-test`: turn policy, speculative partial handling, barge-in/echo filtering, assistant turn cancellation, OpenAI streaming, ElevenLabs Flash playback, and conversation history.
- Keep only the `switch_voice` OpenAI tool-call handling in V1. It is intentionally a proof of concept for later robot behavior tools such as movement or navigation.
- Do not paste the proof-of-concept script wholesale into `src/`. Split it into small modules with clear ownership.
- Publish status through the existing telemetry hub. Do not add a second dashboard data path.
- Route assistant speech through the ReSpeaker playback device when echo cancellation matters.
- Capture from the ReSpeaker explicitly. Avoid relying on the ambient system default audio device.
- Do not let voice tools talk to motors directly. Future robot behavior tools should hand structured commands to `robot-brain` or the future ROS2 behavior/action layer.

Target shape:

```text
robot-web-dashboard
  -> GET/POST /config/voice
  -> writes /home/pi/.config/robot-pet/voice.json
  -> shows voice telemetry

robot-voice.service
  -> owns ReSpeaker capture/playback while listening
  -> streams selected mic channel to ElevenLabs Scribe
  -> streams OpenAI response to ElevenLabs Flash
  -> publishes voice state to robot-telemetry

robot-telemetry.service
  -> includes voice source in dashboard snapshots
```

## Open Validation Points

- Whether Python `sounddevice` / PortAudio can reliably open the direct ALSA hardware capture path as `hw:0,0` with 6 channels on the Pi in the service runtime.
- Whether `hw:0,0` is stable enough to configure directly, or whether the service should resolve the ReSpeaker by device name at startup.
- Whether playback through `plughw:0,0` gives acceptable echo cancellation during assistant speech in the full cloud loop.
- Whether XVF3800 tuning through `xvf_host` is needed. Do not add this until the basic capture/playback path is measured.

Already validated on the robot:

- ReSpeaker microphone records audio.
- Channel index `1` is the correct initial selected channel.
- ReSpeaker speaker playback works.

## Confirmed Direction

These are intentional decisions for this migration:

- Use a separate always-running `robot-voice` systemd service.
- Dashboard control writes config; it does not start/stop the unit.
- Use ElevenLabs Scribe for realtime STT, OpenAI Responses for assistant/tool calls, and ElevenLabs Flash for TTS.
- Use the ReSpeaker for both capture and assistant playback.
- Keep the speculative turn policy, barge-in handling, assistant cancellation, conversation history, and voice-switch tool from the proof of concept.
- Start with 16 kHz PCM, 6-channel ReSpeaker capture, selected mono channel extraction, and configurable channel index `1`.

## Stage 1 - Move Voice Core Into Robo-Pet

Create a small `src/voice/` package and move the reusable proof-of-concept logic into testable modules. Keep the first pass flatter than the proof-of-concept boundaries suggest; split files later only when the code is genuinely hard to follow.

Proposed modules:

- `src/voice/conversation.py`
  - `ConversationHistory`
  - exchange retention
- `src/voice/turn_policy.py`
  - transcript normalization
  - speculative partial decision
  - commit decision
  - barge-in decision
  - assistant echo suppression
- `src/voice/assistant.py`
  - `ActiveTurn`
  - assistant turn lifecycle
  - cancellation and playback gating
  - OpenAI response streaming
  - voice switch tool handling
- `src/voice/elevenlabs_io.py`
  - Scribe realtime websocket
  - Flash TTS websocket
  - text/audio streaming glue
- `src/voice/session.py`
  - one small lifecycle owner for active async tasks
  - start/stop listening session
  - cancellation and cleanup when config disables or changes
- `src/drivers/respeaker.py`
  - pure ReSpeaker capture/playback driver
  - channel extraction helpers
- `src/robot_voice.py`
  - thin config-driven service entrypoint
  - owns args, signals, config polling, env loading, and telemetry publishing
  - starts/stops `VoiceSession`; do not let this file become the whole voice application

Implementation notes:

- Replace proof-of-concept `print` JSON logging with `lib.log.setup_logging("robot-voice")`.
- Keep cloud API code injectable enough that tests can fake OpenAI, ElevenLabs, and audio devices.
- Keep hardware access framework-agnostic in `src/drivers/` so it can move into a ROS2 node later without carrying systemd/dashboard assumptions.
- Keep audio ownership explicit: OpenAI/Scribe/Flash code consumes or produces PCM bytes only. It must not open `sounddevice` streams directly. `src/drivers/respeaker.py` owns PortAudio/ALSA access, and `robot_voice.py` wires that driver into the voice pipeline.
- Do not introduce ROS2 concepts yet. Follow the current service scaffolding style.
- Keep the lifecycle boundary simple. `VoiceSession` is allowed because it owns real concurrency state: audio streams, websockets, assistant turns, cancellation, and cleanup. Do not add managers, factories, base classes, plugin systems, or a generic audio bus.

Tests:

- Port the existing `elevenlabs-test` tests for:
  - conversation history
  - turn policy
  - active turn cancellation
  - assistant turn streaming
  - Scribe event handling
- Keep these tests independent of real audio hardware and network.
- Convert the pytest-style proof-of-concept tests to the repo's current `unittest` style, or explicitly switch the repo to pytest in setup/redeploy. Prefer conversion for V1 because existing robo-pet tests and setup use `python -m unittest`.

Acceptance:

- `python -m unittest discover tests` passes.
- Voice core modules can be imported without opening audio devices or network sockets.

## Stage 2 - Add Voice Config

Add persistent voice config following the style of `src/config/teleop.py` and `src/config/vision.py`.

Implementation:

- Create `src/config/voice.py`.
- Store config at `/home/pi/.config/robot-pet/voice.json`.
- Add a frozen dataclass, likely `VoiceConfig`, with:
  - `enabled: bool = False`
  - `input_device: str = "hw:0,0"`
  - `output_device: str = "plughw:0,0"`
  - `sample_rate: int = 16000`
  - `capture_channels: int = 6`
  - `capture_channel_index: int = 1`
  - `output_channels: int = 1`
  - `voice_id: str | None = None`
  - `alternate_voice_id: str | None = None`
- Clamp or validate:
  - `sample_rate` initially only `16000`
  - `capture_channels` initially only `6`
  - `capture_channel_index` in `0..5`
  - `output_channels` initially `1` or whatever playback testing proves necessary
- Treat missing config as defaults.
- Raise a clear config error for malformed JSON or non-object JSON.
- Use atomic save with temp file plus `os.replace`.

Secrets:

- Do not store API keys in `voice.json`.
- Use a Pi-local environment file such as `/home/pi/.config/robot-pet/voice.env`.
- Add `EnvironmentFile=-/home/pi/.config/robot-pet/voice.env` to `robot-voice.service`.
- Expected variables:
  - `ELEVENLABS_API_KEY`
  - `OPENAI_API_KEY`

Tests:

- Missing file returns defaults.
- Save/load round trip.
- Malformed JSON raises voice config error.
- Non-object JSON raises voice config error.
- Channel index validation.
- Optional string fields preserve values.

Defer editable system prompt config for V1. Keep the system prompt in code until there is a real need to edit it from the dashboard.

Acceptance:

- `python -m unittest tests.test_voice_config` passes.
- No service code is required for this stage.

## Stage 3 - Add Voice Telemetry Shape

Extend existing telemetry so `robot-voice` can publish small JSON updates and the dashboard can receive them through `/events`.

Implementation:

- In `src/telemetry/messages.py`, add `voice_update(...)`.
- Use source name exactly `voice`.
- In `src/robot_telemetry.py`, include `voice` in `sources`.
- Include top-level `voice` in snapshots, using the latest `voice` source data.
- Keep telemetry generic. Do not create a special voice socket.

Payload contract:

```json
{
  "type": "source_update",
  "source": "voice",
  "time": 1770000000.0,
  "enabled": true,
  "status": "listening",
  "input_device": "hw:0,0",
  "output_device": "plughw:0,0",
  "sample_rate": 16000,
  "capture_channels": 6,
  "capture_channel_index": 1,
  "assistant_speaking": false,
  "partial_transcript": "what is",
  "last_committed_transcript": "what is your name",
  "last_assistant_text": "I am Bloop.",
  "last_error": null
}
```

Allowed initial `status` values:

- `disabled`
- `starting`
- `listening`
- `hearing`
- `thinking`
- `speaking`
- `reconnecting`
- `error`

Tests:

- Add telemetry message tests for `voice_update`.
- Extend telemetry hub tests so snapshots include:
  - `sources.voice`
  - top-level `voice`
  - stale status when no voice update has arrived
  - non-stale status after a voice update

Acceptance:

- Existing gamepad, vision, and system telemetry behavior is unchanged.
- `python -m unittest tests.test_telemetry_messages tests.test_robot_telemetry` passes.

## Stage 4 - Implement ReSpeaker Audio Driver And Adapter

Add audio code that can capture from the current 6-channel ReSpeaker stream and select one channel explicitly.

Implementation:

- In `src/drivers/respeaker.py`, add pure driver classes/functions that:
  - opens configured input device
  - requests `16000 Hz`, `S16_LE`, `6` channels
  - reads interleaved PCM frames
  - extracts configured zero-based channel index into mono PCM
  - opens configured output device
  - writes mono 16 kHz PCM from ElevenLabs Flash
  - avoids using HDMI/Bluetooth/default output accidentally
- In `robot_voice.py`, add only the small async glue needed to feed captured PCM into Scribe and write Flash PCM to the driver.
- Add startup logging for:
  - selected input device
  - selected output device
  - sample rate
  - capture channels
  - selected channel index
- Add optional device probing helper for diagnostics, but keep config explicit for V1.

Important:

- Do not manually mix raw mic channels for V1.
- Do not make `plughw` mono downmix capture the final implementation unless direct 6-channel capture proves unreliable.
- Keep channel index configurable so `0` and `1` can be compared on the real robot.
- Do not add a general audio mixer or sound-effect path in V1. This service is for robot voice only.

Tests:

- Feed synthetic interleaved 6-channel PCM into the channel selector.
- Verify channel `0`, channel `1`, and channel `5` extraction.
- Verify invalid channel index fails early.
- Test that output writer can be faked without real PortAudio.

Acceptance:

- Unit tests prove channel extraction byte-for-byte.
- A manual Pi check can record selected channel to a WAV file or log RMS without calling cloud APIs.

Manual Pi checks:

```bash
aplay -l
arecord -l
arecord -D hw:0,0 --dump-hw-params -f S16_LE -r 16000 -c 6 -d 1 /dev/null
speaker-test -D plughw:0,0 -c 1 -t wav
```

## Stage 5 - Add Robot Voice Service

Create a long-running service that applies config live.

Implementation:

- Add `src/robot_voice.py`.
- In `src/voice/session.py`, implement the active voice session lifecycle:
  - opens and closes the configured audio/cloud paths
  - starts capture/Scribe/assistant tasks
  - cancels active turn and closes sockets when stopped
  - reconnects with bounded backoff after transient network/audio failures
- In `src/robot_voice.py`, keep only the service wrapper lifecycle:
  - polls `voice.json` for changes
  - stays idle when `enabled` is false
  - validates required API key environment variables before opening audio/cloud sessions
  - starts `VoiceSession` when `enabled` becomes true
  - stops `VoiceSession` when disabled or when audio-affecting config changes
  - publishes telemetry during every state transition
  - publishes clear `error` telemetry for missing/invalid credentials and API authentication failures
- Add `systemd/robot-voice.service`.
- Unit dependencies:
  - `After=network-online.target robot-telemetry.service`
  - `Wants=network-online.target robot-telemetry.service`
- Use the same working directory and `PYTHONPATH` pattern as the other services.

Disabled behavior:

- Microphone stream closed.
- Scribe websocket closed.
- Active assistant turn cancelled.
- Playback stopped.
- Telemetry status is `disabled`.

Enabled behavior:

- ReSpeaker input/output opened.
- Scribe websocket connected.
- Partial and committed transcripts processed.
- OpenAI turns started from committed or stable speculative transcripts.
- OpenAI tool calls are handled by the voice layer. V1 only supports the `switch_voice` tool. Unknown tool calls are logged and rejected; they must not be treated as voice switches. Future robot behavior tools should emit structured robot commands for `robot-brain` or ROS2, not directly control hardware.
- TTS played through the configured ReSpeaker output device.

Tests:

- Disabled config does not open audio or network.
- Enabling starts the configured fake audio/Scribe path.
- Disabling cancels active listen and active assistant turn.
- Config change from channel `1` to channel `0` restarts capture cleanly.
- Missing required API key publishes `error` and does not open audio or cloud sockets.
- API authentication failure publishes `error` with a useful `last_error`.
- Network failure publishes `reconnecting` or `error` and retries.

Acceptance:

- `VoiceSession` can run with fake clients in tests.
- Service shutdown handles `SIGTERM` without leaking tasks.

## Stage 6 - Dashboard Control

Add dashboard config and status for voice.

Implementation:

- In `robot_web_dashboard.py`, add:
  - `GET /config/voice`
  - `POST /config/voice`
- Extend `WebDashboardState` with `voice_config_path`.
- Add voice fields to the existing config modal:
  - Listen enabled
  - input device
  - output device
  - capture channel index
  - voice ID
  - alternate voice ID
- Update the dashboard config renderer before adding these fields:
  - support explicit `type: "text"` fields
  - preserve string values instead of coercing every non-checkbox input with `Number(...)`
  - post string fields unchanged
- Add voice status rows to the dashboard:
  - status
  - input device
  - output device
  - selected channel
  - last transcript
  - last error
- Keep SSH TUI integration out of V1 unless it is trivial after web dashboard support.

Acceptance:

- Toggling Listen writes `voice.json`.
- `robot-voice` applies the toggle without a service restart.
- Dashboard shows stale/error states when `robot-voice` is stopped or broken.
- Dashboard config rendering supports string fields for device names and voice IDs, and existing numeric drive/vision fields still post as numbers.

## Stage 7 - Setup And Redeploy Integration

Update installation and redeploy scaffolding.

Implementation:

- Add dependencies to `pyproject.toml`:
  - `openai`
  - `websockets`
  - `sounddevice`
  - `certifi`
- Add `voice` to packaged packages and `robot_voice` to packaged modules, or switch packaging to discovery.
- Add any needed apt packages to `setup.sh`:
  - `alsa-utils` for diagnostics
  - `sox` for the manual channel comparison checklist
  - `portaudio19-dev` only if the Pi install path requires it for `sounddevice`
- Ensure the `pi` user running under systemd can open the ReSpeaker devices:
  - add the user to `audio` if needed on the target Raspberry Pi OS image
  - include a service-runtime audio smoke check in the hardware checklist
- Install and enable `robot-voice.service`.
- Include `robot-voice.service` in redeploy stop/start/restart lists.
- Include `robot-voice.service` in dashboard log streaming.
- Add sudoers permissions for every `robot-voice.service` systemctl action used by setup/redeploy (`enable`, `start`, `stop`, and `restart`) and for any new apt install command used by redeploy.
- Document where to create `/home/pi/.config/robot-pet/voice.env`.

Acceptance:

- Fresh Pi setup installs dependencies and enables `robot-voice.service`.
- Redeploy restarts `robot-voice.service` with the other robot services.
- Dashboard logs include voice service logs.

## Stage 8 - Hardware Bring-Up Checklist

These checks have already validated the initial robot hardware path. Keep them here as a repeatable checklist for a fresh Pi, a different ReSpeaker, changed USB ordering, or debugging before cloud integration.

Confirm USB device:

```bash
lsusb
```

Expected device includes:

```text
2886:001e Seeed Technology Co., Ltd. reSpeaker Flex XVF3800 C16K6Ch
```

Confirm ALSA devices:

```bash
aplay -l
arecord -l
```

Confirm playback:

```bash
speaker-test -D plughw:0,0 -c 1 -t wav
```

Confirm the systemd service user can access audio devices:

```bash
sudo -u pi arecord -D hw:0,0 --dump-hw-params -f S16_LE -r 16000 -c 6 -d 1 /dev/null
sudo -u pi speaker-test -D plughw:0,0 -c 1 -t wav
```

Confirm direct hardware capture format:

```bash
arecord -D hw:0,0 --dump-hw-params -f S16_LE -r 16000 -c 6 -d 1 /dev/null
```

Record 6-channel audio:

```bash
arecord -D hw:0,0 -f S16_LE -r 16000 -c 6 -d 5 respeaker-6ch.wav
```

Compare processed channels manually:

```bash
sox respeaker-6ch.wav ch1.wav remix 1
sox respeaker-6ch.wav ch2.wav remix 2
aplay -D plughw:0,0 ch1.wav
aplay -D plughw:0,0 ch2.wav
```

Interpretation:

- SoX `remix 1` is zero-based channel index `0`.
- SoX `remix 2` is zero-based channel index `1`.

Acceptance:

- Initial `capture_channel_index` is `1`.
- If hardware changes, re-run the checklist and record any changed result in this plan or a follow-up decision note.

## Stage 9 - Later / Only If Needed

- Wake word activation.
  - Replace or augment dashboard Listen toggle.
  - Reuse the same ReSpeaker driver and service state machine.
- `xvf_host` integration.
  - Firmware version.
  - DoA / azimuth.
  - speech energy.
  - LED control.
  - mute/GPIO.
  - tuning parameters.
- ReSpeaker tuning.
  - Consider only after baseline capture/playback is measured.
  - Candidate parameters include reference gain, mic gain, system delay, AGC max gain, and ASR output gain.
- Conversation reset control.
  - Useful once long-running voice sessions are common.
- ROS2 migration.
  - `src/drivers/respeaker.py`, `turn_policy.py`, and conversation logic should survive.
  - `robot_voice.py` and systemd service wrapper remain temporary scaffolding.

## Recommended First Milestone

Smallest useful milestone:

- Add `robot-voice.service`.
- Add dashboard Listen toggle.
- Capture 6-channel ReSpeaker audio.
- Select configurable processed channel.
- Stream selected mono audio to ElevenLabs Scribe.
- Stream OpenAI response to ElevenLabs Flash.
- Play back through the ReSpeaker.
- Publish basic telemetry: `disabled`, `listening`, `thinking`, `speaking`, `error`.

Stop there before wake word, LEDs, DoA, firmware changes, or DSP tuning.
