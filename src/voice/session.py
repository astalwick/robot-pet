from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from typing import Any

from config.voice import VoiceConfig
from drivers.respeaker import ReSpeakerAudio
from lib.log import setup_logging
from voice.assistant import AudioLevels, VoiceState, compose_system_prompt, handle_scribe_events, run_assistant_turn
from voice.conversation import ConversationHistory
from voice.elevenlabs_io import stream_audio_to_scribe
from voice.personality import load_personalities, lookup_personality
from voice.turn_policy import turn_policy_from_config


log = setup_logging("robot-voice")

StatusCallback = Callable[[dict[str, object]], None]


def playback_rms_with_gain(rms: int, gain: float) -> int:
    return min(32767, int(rms * gain))


class VoiceSession:
    def __init__(
        self,
        config: VoiceConfig,
        elevenlabs_api_key: str,
        openai_client: Any,
        status_callback: StatusCallback,
        audio: ReSpeakerAudio,
        scribe_streamer: Callable[..., Any] = stream_audio_to_scribe,
        event_callback: Callable[[dict[str, object]], None] | None = None,
        motion_intent_caller: Callable[[str], Any] | None = None,
        session_end_caller: Callable[[], Any] | None = None,
        camera_snapshot_caller: Callable[[], bytes] | None = None,
        personalities: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self.config = config
        self.elevenlabs_api_key = elevenlabs_api_key
        self.openai_client = openai_client
        self.status_callback = status_callback
        self.event_callback = event_callback
        self.audio = audio
        self.scribe_streamer = scribe_streamer
        self.motion_intent_caller = motion_intent_caller
        self.session_end_caller = session_end_caller
        self.camera_snapshot_caller = camera_snapshot_caller
        self.stop_event = asyncio.Event()
        self._mic_frames: AsyncIterator[bytes] | None = None
        self.tasks: list[asyncio.Task[Any]] = []
        self.scribe_events: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        card_map = personalities if personalities is not None else load_personalities()
        self.personality_name, voice_id, prose = lookup_personality(config.personality, card_map)
        self.system_prompt = compose_system_prompt(prose)
        self.voice_state = VoiceState(voice_id=voice_id)
        log.info("personality: %s (voice %s)", self.personality_name, voice_id)

        self.history = ConversationHistory()
        self.policy = turn_policy_from_config(config)
        self.audio_levels = AudioLevels()

    async def start(self) -> None:
        self.stop_event.clear()
        self.status_callback(
            {
                "status": "starting",
                "assistant_speaking": False,
                "last_error": None,
                "personality": self.personality_name,
            }
        )
        self._mic_frames = self.audio.mic_frames(self.stop_event)

        async def assistant_runner(turn_id, openai_input, playback_event, speaking_event, *args, **kwargs):
            async def turn_speaker(text_chunks, api_key, voice_id, playback_event, speaking_event):
                await self._speak(text_chunks, api_key, voice_id, playback_event, speaking_event, turn_id)
            return await run_assistant_turn(
                turn_id, openai_input, playback_event, speaking_event, *args,
                **kwargs, tts_speaker=turn_speaker,
            )

        self.tasks = [
            asyncio.create_task(
                self.scribe_streamer(
                    self._mic_frames,
                    self.scribe_events,
                    self.elevenlabs_api_key,
                    self.config.sample_rate,
                    policy=self.policy,
                    audio_levels=self.audio_levels,
                )
            ),
            asyncio.create_task(
                handle_scribe_events(
                    self.scribe_events,
                    self.openai_client,
                    self.elevenlabs_api_key,
                    self.voice_state,
                    self.stop_event,
                    self.system_prompt,
                    policy=self.policy,
                    audio_levels=self.audio_levels,
                    conversation_history=self.history,
                    on_status=self.status_callback,
                    on_event=self.event_callback,
                    assistant_runner=assistant_runner,
                    motion_intent_caller=self.motion_intent_caller,
                    session_end_caller=self.session_end_caller,
                    camera_snapshot_caller=self.camera_snapshot_caller,
                    stop_playback_now=self.stop_playback_now,
                )
            ),
        ]
        self.status_callback({"status": "listening", "assistant_speaking": False})

    async def stop(self) -> None:
        self.stop_event.set()
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task
        self.tasks = []
        self._mic_frames = None

    async def wait(self) -> None:
        if not self.tasks:
            return
        done, _pending = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.result()

    def stop_playback_now(self) -> None:
        self.audio.stop_playback_now()
        self.audio_levels.playback_rms = 0
        self.audio_levels.playback_at = 0.0

    async def _speak(self, text_chunks, elevenlabs_api_key, voice_id, playback_event, speaking_event, turn_id):
        from voice.elevenlabs_io import speak_with_eleven_flash

        speaking_started = False

        def on_playback_rms(rms: int) -> None:
            nonlocal speaking_started
            loop = asyncio.get_running_loop()
            self.audio_levels.playback_rms = playback_rms_with_gain(rms, self.config.output_gain)
            self.audio_levels.playback_at = loop.time()
            if not speaking_started:
                speaking_started = True
                now = time.monotonic()
                self.status_callback({"status": "speaking", "assistant_speaking": True})
                if self.event_callback:
                    self.event_callback({"type": "phase", "t": now, "name": "speaking", "on": True})
                    self.event_callback({"type": "assistant_start", "t": now, "turn_id": turn_id})

        playback_id = await self.audio.begin_playback()
        cancelled = False
        try:
            await speak_with_eleven_flash(
                text_chunks,
                elevenlabs_api_key,
                voice_id,
                playback_event,
                speaking_event,
                audio_writer=self.audio.write_output,
                on_playback_rms=on_playback_rms,
            )
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            await self.audio.end_playback(playback_id, drain=not cancelled)
            if speaking_started:
                self.status_callback({"status": "listening", "assistant_speaking": False})
                if self.event_callback:
                    self.event_callback({"type": "phase", "t": time.monotonic(), "name": "speaking", "on": False})
