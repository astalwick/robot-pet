from __future__ import annotations

import asyncio
import base64
import json
import ssl
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from urllib.parse import urlencode

from lib.log import setup_logging
from voice.assistant import AudioLevels
from voice.assistant import VoiceSwitch
from voice.assistant import note_mic_chunk
from voice.turn_policy import DEFAULT_TURN_POLICY, USER_ACTIVE_RMS_THRESHOLD, pcm16_rms


log = setup_logging("robot-voice")

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
SCRIBE_RECONNECT_BASE_SECS = 0.2
SCRIBE_RECONNECT_MAX_SECS = 2.0


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


def log_elevenlabs_payload(data: object, context: str) -> None:
    if not isinstance(data, dict):
        return
    message_type = data.get("message_type", "")
    if message_type in {"error", "auth_error", "quota_exceeded", "input_error"}:
        log.warning("elevenlabs %s: %s", context, data)
        return
    if data.get("error") or data.get("detail"):
        log.warning("elevenlabs %s: %s", context, data)


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
    audio_levels: AudioLevels | None = None,
    profile_every: int = 0,
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

    reconnect_delay = SCRIBE_RECONNECT_BASE_SECS
    while True:
        try:
            async with websockets.connect(
                url,
                additional_headers={"xi-api-key": elevenlabs_api_key},
                ssl=ssl_context(),
            ) as ws:
                session_message = json.loads(await ws.recv())
                log_elevenlabs_payload(session_message, "scribe session")
                reconnect_delay = SCRIBE_RECONNECT_BASE_SECS

                async def receive_transcripts() -> None:
                    async for message in ws:
                        data = json.loads(message)
                        log_elevenlabs_payload(data, "scribe")
                        message_type = data.get("message_type")
                        text = (data.get("text") or "").strip()

                        if message_type == "partial_transcript" and text:
                            await scribe_events.put({"type": "partial", "text": text})
                        elif message_type in {"committed_transcript", "committed_transcript_with_timestamps"} and text:
                            await scribe_events.put({"type": "commit", "text": text})

                last_activity_log_at = 0.0
                last_above_at: float | None = None
                profile_count = 0
                loop = asyncio.get_running_loop()
                receive_task = asyncio.create_task(receive_transcripts())
                try:
                    async for chunk in audio_chunks:
                        if receive_task.done():
                            receive_task.result()
                        started = time.perf_counter()
                        gate_started = time.perf_counter()
                        rms = pcm16_rms(chunk)
                        now = loop.time()
                        gate_open, last_above_at = update_scribe_upload_gate(now, rms, last_above_at)
                        if audio_levels is not None:
                            note_mic_chunk(audio_levels, rms)
                            audio_levels.scribe_gate_open = gate_open
                        if now - last_activity_log_at >= LOCAL_SPEECH_LOG_INTERVAL_SECS:
                            await scribe_events.put({"type": "audio_activity", "rms": rms})
                            last_activity_log_at = now
                        gate_seconds = time.perf_counter() - gate_started

                        encode_started = time.perf_counter()
                        upload = chunk if gate_open else b"\x00" * len(chunk)
                        message = json.dumps(
                            {
                                "message_type": "input_audio_chunk",
                                "audio_base_64": base64.b64encode(upload).decode("ascii"),
                                "commit": False,
                                "sample_rate": sample_rate,
                            }
                        )
                        encode_seconds = time.perf_counter() - encode_started

                        send_started = time.perf_counter()
                        await ws.send(message)
                        if receive_task.done():
                            receive_task.result()
                        send_seconds = time.perf_counter() - send_started
                        profile_count += 1
                        if profile_every > 0 and profile_count % profile_every == 0:
                            log.info(
                                "voice scribe profile: gate_open=%s rms=%d gate=%.1fms encode=%.1fms send=%.1fms total=%.1fms",
                                gate_open,
                                rms,
                                gate_seconds * 1000,
                                encode_seconds * 1000,
                                send_seconds * 1000,
                                (time.perf_counter() - started) * 1000,
                            )
                    await receive_task
                finally:
                    if not receive_task.done():
                        receive_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await receive_task
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("elevenlabs scribe failed; reconnecting in %.1fs: %s", reconnect_delay, exc)
            if audio_levels is not None:
                audio_levels.scribe_gate_open = False
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, SCRIBE_RECONNECT_MAX_SECS)


async def speak_with_eleven_flash(
    text_chunks: AsyncIterator[str | VoiceSwitch],
    elevenlabs_api_key: str,
    voice_id: str,
    playback_event: asyncio.Event,
    speaking_event: asyncio.Event,
    audio_writer: Callable[[bytes], object] | None = None,
    on_playback_rms: Callable[[int], None] | None = None,
    profile_every: int = 0,
) -> None:
    import websockets

    ws = None
    play_task: asyncio.Task[None] | None = None
    prewarm_task: asyncio.Task | None = None
    current_voice_id = voice_id
    audio_profile_count = 0

    async def write_audio(audio: bytes) -> None:
        nonlocal audio_profile_count
        started = time.perf_counter()
        if not speaking_event.is_set():
            speaking_event.set()
        rms_seconds = 0.0
        if on_playback_rms:
            rms_started = time.perf_counter()
            on_playback_rms(pcm16_rms(audio))
            rms_seconds = time.perf_counter() - rms_started
        write_seconds = 0.0
        if audio_writer:
            write_started = time.perf_counter()
            result = audio_writer(audio)
            if asyncio.iscoroutine(result):
                await result
            write_seconds = time.perf_counter() - write_started
        audio_profile_count += 1
        if profile_every > 0 and audio_profile_count % profile_every == 0:
            log.info(
                "voice tts profile: bytes=%d rms=%.1fms write=%.1fms total=%.1fms",
                len(audio),
                rms_seconds * 1000,
                write_seconds * 1000,
                (time.perf_counter() - started) * 1000,
            )

    async def open_voice_socket(next_voice_id: str):
        query = urlencode({"model_id": ELEVEN_FLASH_MODEL, "output_format": "pcm_16000"})
        url = f"wss://api.elevenlabs.io/v1/text-to-speech/{next_voice_id}/stream-input?{query}"
        next_ws = None
        try:
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
        except asyncio.CancelledError:
            if next_ws:
                with suppress(TimeoutError):
                    await asyncio.wait_for(next_ws.close(), timeout=ELEVEN_CLOSE_TIMEOUT_SECS)
            raise
        except Exception:
            if next_ws:
                with suppress(TimeoutError):
                    await asyncio.wait_for(next_ws.close(), timeout=ELEVEN_CLOSE_TIMEOUT_SECS)
            raise

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
            except websockets.exceptions.ConnectionClosed as exc:
                log.warning("elevenlabs tts connection closed: code=%s reason=%s", exc.code, exc.reason)
                break

            data = json.loads(message)
            log_elevenlabs_payload(data, "tts")
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

    def prewarm_voice_socket() -> None:
        nonlocal prewarm_task
        if ws is None and prewarm_task is None:
            prewarm_task = asyncio.create_task(open_voice_socket(current_voice_id))

    async def cancel_prewarm_socket() -> None:
        nonlocal prewarm_task
        if prewarm_task is None:
            return
        task = prewarm_task
        prewarm_task = None
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            prewarmed_ws = await task
            with suppress(TimeoutError):
                await asyncio.wait_for(prewarmed_ws.close(), timeout=ELEVEN_CLOSE_TIMEOUT_SECS)

    async def ensure_voice_socket() -> None:
        nonlocal play_task, prewarm_task, ws
        if ws is None:
            if prewarm_task:
                try:
                    ws = await prewarm_task
                    play_task = asyncio.create_task(play_audio(ws))
                except Exception as exc:
                    log.warning("elevenlabs tts prewarm failed; retrying: %s", exc)
                    await start_voice_socket(current_voice_id)
                finally:
                    prewarm_task = None
            else:
                await start_voice_socket(current_voice_id)

    async def send_text_chunk(chunk: str) -> None:
        nonlocal ws
        for attempt in range(2):
            await ensure_voice_socket()
            try:
                await ws.send(json.dumps({"text": chunk, "try_trigger_generation": True}))
                return
            except Exception:
                if attempt:
                    raise
                log.warning("elevenlabs tts send failed; retrying")
                await cancel_voice_socket()

    async def finish_voice_socket() -> None:
        nonlocal play_task, ws
        if ws is None:
            await cancel_prewarm_socket()
            return
        await ws.send(json.dumps({"text": ""}))
        await play_task
        await ws.close()
        play_task = None
        ws = None

    async def cancel_voice_socket() -> None:
        nonlocal play_task, prewarm_task, ws
        await cancel_prewarm_socket()
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
        prewarm_voice_socket()
        async for chunk in text_chunks:
            if isinstance(chunk, VoiceSwitch):
                await finish_voice_socket()
                current_voice_id = chunk.voice_id
                prewarm_voice_socket()
                continue

            await send_text_chunk(chunk)

        await finish_voice_socket()
    except asyncio.CancelledError:
        await cancel_voice_socket()
        raise
    except Exception as exc:
        log.warning("elevenlabs tts failed: %s", exc)
        await cancel_voice_socket()
        raise
    finally:
        speaking_event.clear()
