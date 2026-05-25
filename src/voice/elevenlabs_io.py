from __future__ import annotations

import asyncio
import base64
import json
import ssl
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from urllib.parse import urlencode

from voice.assistant import VoiceSwitch
from voice.assistant import note_mic_chunk
from voice.turn_policy import DEFAULT_TURN_POLICY, USER_ACTIVE_RMS_THRESHOLD, pcm16_rms


SAMPLE_RATE = 16000
SCRIBE_MODEL = "scribe_v2_realtime"
ELEVEN_FLASH_MODEL = "eleven_flash_v2_5"
ELEVEN_CLOSE_TIMEOUT_SECS = 0.2
SCRIBE_VAD_SILENCE_THRESHOLD_SECS = 1.0
SCRIBE_VAD_THRESHOLD = 0.6
SCRIBE_MIN_SPEECH_DURATION_MS = 200
SCRIBE_MIN_SILENCE_DURATION_MS = 300
LOCAL_SPEECH_LOG_INTERVAL_SECS = 0.35
MIC_SCRIBE_SEND_RMS_MIN = USER_ACTIVE_RMS_THRESHOLD
MIC_SCRIBE_GATE_HOLD_SECS = 1


def update_scribe_upload_gate(
    now: float,
    rms: int,
    last_above_at: float | None,
) -> tuple[bool, float | None]:
    if rms > MIC_SCRIBE_SEND_RMS_MIN:
        return True, now
    if last_above_at is not None and (now - last_above_at) < MIC_SCRIBE_GATE_HOLD_SECS:
        return True, last_above_at
    return False, None


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except ModuleNotFoundError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


async def stream_audio_to_scribe(
    audio_chunks: AsyncIterator[bytes],
    scribe_events: asyncio.Queue[dict[str, object]],
    elevenlabs_api_key: str,
    sample_rate: int = SAMPLE_RATE,
    policy=DEFAULT_TURN_POLICY,
    audio_levels: dict[str, float | int] | None = None,
) -> None:
    import websockets

    query = urlencode(
        {
            "model_id": SCRIBE_MODEL,
            "audio_format": "pcm_16000",
            "commit_strategy": "vad",
            "vad_silence_threshold_secs": str(SCRIBE_VAD_SILENCE_THRESHOLD_SECS),
            "vad_threshold": str(SCRIBE_VAD_THRESHOLD),
            "min_speech_duration_ms": str(SCRIBE_MIN_SPEECH_DURATION_MS),
            "min_silence_duration_ms": str(SCRIBE_MIN_SILENCE_DURATION_MS),
        }
    )
    url = f"wss://api.elevenlabs.io/v1/speech-to-text/realtime?{query}"

    async with websockets.connect(
        url,
        additional_headers={"xi-api-key": elevenlabs_api_key},
        ssl=ssl_context(),
    ) as ws:
        await ws.recv()

        async def send_audio() -> None:
            last_activity_log_at = 0.0
            last_above_at: float | None = None
            loop = asyncio.get_running_loop()

            async for chunk in audio_chunks:
                rms = pcm16_rms(chunk)
                now = loop.time()
                gate_open, last_above_at = update_scribe_upload_gate(now, rms, last_above_at)
                if audio_levels is not None:
                    note_mic_chunk(audio_levels, rms)
                    audio_levels["scribe_gate_open"] = 1 if gate_open else 0
                if now - last_activity_log_at >= LOCAL_SPEECH_LOG_INTERVAL_SECS:
                    await scribe_events.put({"type": "audio_activity", "rms": rms})
                    last_activity_log_at = now

                upload = chunk if gate_open else b"\x00" * len(chunk)
                await ws.send(
                    json.dumps(
                        {
                            "message_type": "input_audio_chunk",
                            "audio_base_64": base64.b64encode(upload).decode("ascii"),
                            "commit": False,
                            "sample_rate": sample_rate,
                        }
                    )
                )

        async def receive_transcripts() -> None:
            async for message in ws:
                data = json.loads(message)
                message_type = data.get("message_type")
                text = (data.get("text") or "").strip()

                if message_type == "partial_transcript" and text:
                    await scribe_events.put({"type": "partial", "text": text})
                elif message_type in {"committed_transcript", "committed_transcript_with_timestamps"} and text:
                    await scribe_events.put({"type": "commit", "text": text})

        await asyncio.gather(send_audio(), receive_transcripts())


async def speak_with_eleven_flash(
    text_chunks: AsyncIterator[str | VoiceSwitch],
    elevenlabs_api_key: str,
    voice_id: str,
    playback_event: asyncio.Event,
    speaking_event: asyncio.Event,
    audio_writer: Callable[[bytes], object] | None = None,
    on_playback_rms: Callable[[int], None] | None = None,
) -> None:
    import websockets

    ws = None
    play_task: asyncio.Task[None] | None = None
    current_voice_id = voice_id

    async def write_audio(audio: bytes) -> None:
        if not speaking_event.is_set():
            speaking_event.set()
        if on_playback_rms:
            on_playback_rms(pcm16_rms(audio))
        if audio_writer:
            result = audio_writer(audio)
            if asyncio.iscoroutine(result):
                await result

    async def open_voice_socket(next_voice_id: str):
        query = urlencode({"model_id": ELEVEN_FLASH_MODEL, "output_format": "pcm_16000"})
        url = f"wss://api.elevenlabs.io/v1/text-to-speech/{next_voice_id}/stream-input?{query}"
        next_ws = await websockets.connect(url, ssl=ssl_context(), close_timeout=ELEVEN_CLOSE_TIMEOUT_SECS)
        await next_ws.send(
            json.dumps(
                {
                    "text": " ",
                    "voice_settings": {
                        "stability": 0.45,
                        "similarity_boost": 0.85,
                        "use_speaker_boost": False,
                    },
                    "generation_config": {"chunk_length_schedule": [120, 160, 250, 290]},
                    "xi_api_key": elevenlabs_api_key,
                }
            )
        )
        return next_ws

    async def play_audio(active_ws) -> None:
        pending_audio: list[bytes] = []
        final_received = False

        while True:
            if playback_event.is_set() and pending_audio:
                for buffered_audio in pending_audio:
                    await write_audio(buffered_audio)
                pending_audio.clear()

            if final_received and not pending_audio:
                break

            try:
                message = await asyncio.wait_for(active_ws.recv(), timeout=0.05)
            except TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosedOK:
                break
            except websockets.exceptions.ConnectionClosed:
                break

            data = json.loads(message)
            if data.get("audio"):
                audio = base64.b64decode(data["audio"])
                if playback_event.is_set():
                    await write_audio(audio)
                else:
                    pending_audio.append(audio)
            if data.get("isFinal"):
                final_received = True

    async def start_voice_socket(next_voice_id: str) -> None:
        nonlocal play_task, ws
        ws = await open_voice_socket(next_voice_id)
        play_task = asyncio.create_task(play_audio(ws))

    async def ensure_voice_socket() -> None:
        if ws is None:
            await start_voice_socket(current_voice_id)

    async def finish_voice_socket() -> None:
        nonlocal play_task, ws
        if ws is None:
            return
        await ws.send(json.dumps({"text": ""}))
        await play_task
        await ws.close()
        play_task = None
        ws = None

    async def cancel_voice_socket() -> None:
        nonlocal play_task, ws
        if play_task:
            if not play_task.done():
                play_task.cancel()
            with suppress(asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
                await play_task
        if ws:
            with suppress(TimeoutError):
                await asyncio.wait_for(ws.close(), timeout=ELEVEN_CLOSE_TIMEOUT_SECS)
        play_task = None
        ws = None
        speaking_event.clear()

    try:
        async for chunk in text_chunks:
            if isinstance(chunk, VoiceSwitch):
                await finish_voice_socket()
                current_voice_id = chunk.voice_id
                continue

            await ensure_voice_socket()
            await ws.send(json.dumps({"text": chunk, "try_trigger_generation": True}))

        await finish_voice_socket()
    except asyncio.CancelledError:
        await cancel_voice_socket()
        raise
    except Exception:
        await cancel_voice_socket()
        raise
    finally:
        speaking_event.clear()
