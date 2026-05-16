from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from config.voice import VoiceConfig
from drivers.respeaker import ReSpeakerAudio
from voice.assistant import (
    ALTERNATE_VOICE_ID,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_VOICE_ID,
    VoiceState,
    handle_scribe_events,
    run_assistant_turn,
)
from voice.conversation import ConversationHistory
from voice.elevenlabs_io import stream_audio_to_scribe


StatusCallback = Callable[[dict[str, object]], None]


class VoiceSession:
    def __init__(
        self,
        config: VoiceConfig,
        elevenlabs_api_key: str,
        openai_client: Any,
        status_callback: StatusCallback,
        audio: ReSpeakerAudio | None = None,
        scribe_streamer: Callable[..., Any] = stream_audio_to_scribe,
    ) -> None:
        self.config = config
        self.elevenlabs_api_key = elevenlabs_api_key
        self.openai_client = openai_client
        self.status_callback = status_callback
        self.audio = audio or ReSpeakerAudio(
            input_device=config.input_device,
            output_device=config.output_device,
            sample_rate=config.sample_rate,
            capture_channels=config.capture_channels,
            capture_channel_index=config.capture_channel_index,
            output_channels=config.output_channels,
            input_gain=config.input_gain,
            output_gain=config.output_gain,
        )
        self.scribe_streamer = scribe_streamer
        self.stop_event = asyncio.Event()
        self.tasks: list[asyncio.Task[Any]] = []
        self.scribe_events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        voice_id = config.voice_id or DEFAULT_VOICE_ID
        self.voice_state = VoiceState(
            default_voice_id=voice_id,
            alternate_voice_id=config.alternate_voice_id or ALTERNATE_VOICE_ID,
            current_voice_id=voice_id,
        )
        self.history = ConversationHistory()

    async def start(self) -> None:
        self.status_callback({"status": "starting", "assistant_speaking": False, "last_error": None})

        async def assistant_runner(*args, **kwargs):
            monitor = asyncio.create_task(self._status_while_speaking(args[3]))
            try:
                return await run_assistant_turn(*args, **kwargs, tts_speaker=self._speak)
            finally:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor

        self.tasks = [
            asyncio.create_task(
                self.scribe_streamer(
                    self.audio.microphone_chunks(self.stop_event),
                    self.scribe_events,
                    self.elevenlabs_api_key,
                    self.config.sample_rate,
                )
            ),
            asyncio.create_task(
                handle_scribe_events(
                    self.scribe_events,
                    self.openai_client,
                    self.elevenlabs_api_key,
                    self.voice_state,
                    self.stop_event,
                    conversation_history=self.history,
                    system_prompt=DEFAULT_SYSTEM_PROMPT,
                    on_status=self.status_callback,
                    assistant_runner=assistant_runner,
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

    async def wait(self) -> None:
        if not self.tasks:
            return
        done, _pending = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.result()

    async def _speak(self, text_chunks, elevenlabs_api_key, voice_id, playback_event, speaking_event):
        from voice.elevenlabs_io import speak_with_eleven_flash

        await speak_with_eleven_flash(
            text_chunks,
            elevenlabs_api_key,
            voice_id,
            playback_event,
            speaking_event,
            audio_writer=self.audio.write_output,
        )

    async def _status_while_speaking(self, speaking_event: asyncio.Event) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(0.05)
            if speaking_event.is_set():
                self.status_callback({"status": "speaking", "assistant_speaking": True})
                while speaking_event.is_set() and not self.stop_event.is_set():
                    await asyncio.sleep(0.05)
                self.status_callback({"status": "listening", "assistant_speaking": False})
                return
