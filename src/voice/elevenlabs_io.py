from __future__ import annotations

import asyncio
import base64
import json
import ssl
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import cache
from typing import Any
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

# How much local audio to keep before upload starts, so first words aren't clipped,
# and how the socket lingers around a spoken turn. Internal knobs for now (see plan).
SCRIBE_PREROLL_SECS = 0.5
SCRIBE_POST_SPEECH_TAIL_SECS = 1.2
SCRIBE_COMMIT_TIMEOUT_SECS = 2.0
SCRIBE_HOLD_OPEN_SECS = 1.5

# Scribe websocket lifecycle states (separate from the Hey Bloop session lifecycle).
SCRIBE_CLOSED = "closed"
SCRIBE_PREOPEN = "preopen"
SCRIBE_UPLOADING = "uploading"
SCRIBE_WAITING_FOR_COMMIT = "waiting_for_commit"
SCRIBE_HOLD_OPEN = "hold_open"
SCRIBE_RECONNECTING = "reconnecting"


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


@cache
def ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except ModuleNotFoundError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


@dataclass
class _ScribeSocket:
    ws: Any
    receive_task: asyncio.Task[None] | None = None
    got_commit: bool = False


async def stream_audio_to_scribe(
    audio_chunks: AsyncIterator[bytes],
    scribe_events: asyncio.Queue[dict[str, object]],
    elevenlabs_api_key: str,
    sample_rate: int = SAMPLE_RATE,
    policy=DEFAULT_TURN_POLICY,
    audio_levels: AudioLevels | None = None,
    profile_every: int = 0,
    usage: Any = None,
    on_status: Callable[[dict[str, object]], None] | None = None,
    on_event: Callable[[dict[str, object]], None] | None = None,
    vad_silence_threshold_secs: float = SCRIBE_VAD_SILENCE_THRESHOLD_SECS,
    wake_audio: list[bytes] | None = None,
) -> None:
    """Drain local mic audio for the whole active session, opening a Scribe websocket
    only while there is real speech to upload.

    The audio loop is always primary: RMS, ``audio_activity`` events, and the local
    upload gate run every chunk regardless of socket state. The websocket is opened on
    session start (pre-open), reopened on speech, and closed when an utterance has been
    committed (or timed out) and the brief hold-open grace expires.

    ``wake_audio`` is mic audio buffered around the wake word, so speech that ran
    straight through "Hey Bloop" reaches Scribe. It seeds the pre-roll and is
    uploaded once, ahead of the first live speech.
    """
    import websockets

    query = urlencode(
        {
            "model_id": SCRIBE_MODEL,
            "audio_format": "pcm_16000",
            "commit_strategy": "vad",
            "vad_silence_threshold_secs": str(vad_silence_threshold_secs),
            "vad_threshold": str(SCRIBE_VAD_THRESHOLD),
            "min_speech_duration_ms": str(SCRIBE_MIN_SPEECH_DURATION_MS),
            "min_silence_duration_ms": str(SCRIBE_MIN_SILENCE_DURATION_MS),
        }
    )
    url = f"wss://api.elevenlabs.io/v1/speech-to-text/realtime?{query}"

    loop = asyncio.get_running_loop()

    state = SCRIBE_CLOSED
    open_count = 0
    last_error: str | None = None
    link: _ScribeSocket | None = None
    open_task: asyncio.Task[_ScribeSocket | None] | None = None
    open_cooldown_until = 0.0
    reconnect_delay = SCRIBE_RECONNECT_BASE_SECS

    pre_roll: deque[bytes] = deque()
    preroll_frames: int | None = None
    last_above_at: float | None = None
    last_activity_log_at = 0.0
    tail_secs_left = 0.0
    commit_wait_secs_left = 0.0
    hold_secs_left = 0.0
    profile_count = 0

    def publish_status() -> None:
        if on_status is not None:
            on_status(
                {
                    "scribe_state": state,
                    "scribe_open_count": open_count,
                    "scribe_last_error": last_error,
                }
            )

    def emit_event(event_type: str) -> None:
        if on_event is not None:
            on_event({"type": event_type, "t": time.monotonic()})

    def set_state(next_state: str) -> None:
        nonlocal state
        if next_state != state:
            state = next_state
            publish_status()

    async def receive_transcripts(socket: _ScribeSocket) -> None:
        async for message in socket.ws:
            data = json.loads(message)
            log_elevenlabs_payload(data, "scribe")
            message_type = data.get("message_type")
            text = (data.get("text") or "").strip()
            if message_type == "partial_transcript" and text:
                await scribe_events.put({"type": "partial", "text": text})
            elif message_type in {"committed_transcript", "committed_transcript_with_timestamps"} and text:
                socket.got_commit = True
                await scribe_events.put({"type": "commit", "text": text})

    async def connect_scribe() -> _ScribeSocket | None:
        nonlocal last_error, reconnect_delay
        try:
            ws = await websockets.connect(
                url,
                additional_headers={"xi-api-key": elevenlabs_api_key},
                ssl=ssl_context(),
            )
            session_message = json.loads(await ws.recv())
            log_elevenlabs_payload(session_message, "scribe session")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = str(exc)
            log.warning("elevenlabs scribe open failed: %s", exc)
            publish_status()
            return None
        reconnect_delay = SCRIBE_RECONNECT_BASE_SECS
        if last_error is not None:
            last_error = None
            publish_status()
        socket = _ScribeSocket(ws=ws)
        socket.receive_task = asyncio.create_task(receive_transcripts(socket))
        return socket

    def start_open(now: float) -> None:
        nonlocal open_task
        if link is None and open_task is None and now >= open_cooldown_until:
            open_task = asyncio.create_task(connect_scribe())

    def reap_open(now: float) -> None:
        nonlocal open_task, link, open_count, open_cooldown_until, reconnect_delay
        if open_task is None or not open_task.done():
            return
        socket = open_task.result()
        open_task = None
        if socket is None:
            open_cooldown_until = now + reconnect_delay
            reconnect_delay = min(reconnect_delay * 2, SCRIBE_RECONNECT_MAX_SECS)
            return
        link = socket
        open_count += 1
        emit_event("scribe_open")
        publish_status()

    async def close_link(event_type: str) -> None:
        nonlocal link
        if link is None:
            return
        socket = link
        link = None
        if socket.receive_task is not None and not socket.receive_task.done():
            socket.receive_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            if socket.receive_task is not None:
                await socket.receive_task
        with suppress(Exception):
            await socket.ws.close()
        emit_event(event_type)

    async def send_chunk(chunk: bytes, *, silent: bool) -> bool:
        nonlocal last_error
        if link is None:
            return False
        payload = b"\x00" * len(chunk) if silent else chunk
        message = json.dumps(
            {
                "message_type": "input_audio_chunk",
                "audio_base_64": base64.b64encode(payload).decode("ascii"),
                "commit": False,
                "sample_rate": sample_rate,
            }
        )
        try:
            await link.ws.send(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = str(exc)
            publish_status()
            await close_link("scribe_close")
            return False
        if usage is not None:
            usage.stt_audio_seconds += len(chunk) / 2 / sample_rate
        return True

    preopen_started = False
    try:
        async for chunk in audio_chunks:
            now = loop.time()
            chunk_secs = len(chunk) / 2 / sample_rate
            rms = pcm16_rms(chunk)
            gate_open, last_above_at = update_scribe_upload_gate(now, rms, last_above_at)
            if audio_levels is not None:
                note_mic_chunk(audio_levels, rms)
                audio_levels.scribe_gate_open = gate_open
            if now - last_activity_log_at >= LOCAL_SPEECH_LOG_INTERVAL_SECS:
                await scribe_events.put({"type": "audio_activity", "rms": rms})
                last_activity_log_at = now
            if preroll_frames is None:
                preroll_frames = max(1, round(SCRIBE_PREROLL_SECS / max(chunk_secs, 1e-6)))
                # Seed with wake audio, widening the window so it survives until the
                # socket is ready; after the first flush it shrinks back to normal.
                pre_roll = deque(wake_audio or [], maxlen=preroll_frames + len(wake_audio or []))
            pre_roll.append(chunk)

            if not preopen_started:
                preopen_started = True
                start_open(now)

            if link is not None and link.receive_task is not None and link.receive_task.done():
                reconnect = state == SCRIBE_UPLOADING or gate_open
                await close_link("scribe_close")
                if reconnect:
                    emit_event("scribe_reconnect")
                    set_state(SCRIBE_RECONNECTING)
                else:
                    set_state(SCRIBE_CLOSED)
            reap_open(now)

            profile_count += 1
            if profile_every > 0 and profile_count % profile_every == 0:
                log.info("voice scribe profile: state=%s gate_open=%s rms=%d", state, gate_open, rms)

            if gate_open:
                start_open(now)
                reap_open(now)
                if link is not None:
                    if state != SCRIBE_UPLOADING:
                        link.got_commit = False
                        flushed = all([await send_chunk(buffered, silent=False) for buffered in list(pre_roll)])
                        if flushed and pre_roll.maxlen != preroll_frames:
                            pre_roll = deque(pre_roll, maxlen=preroll_frames)
                        set_state(SCRIBE_UPLOADING if flushed else SCRIBE_RECONNECTING)
                    else:
                        if rms > MIC_SCRIBE_SEND_RMS_MIN:
                            link.got_commit = False
                        if not await send_chunk(chunk, silent=False):
                            set_state(SCRIBE_RECONNECTING)
                continue

            if state == SCRIBE_UPLOADING:
                set_state(SCRIBE_WAITING_FOR_COMMIT)
                tail_secs_left = SCRIBE_POST_SPEECH_TAIL_SECS
                commit_wait_secs_left = SCRIBE_COMMIT_TIMEOUT_SECS

            if state == SCRIBE_WAITING_FOR_COMMIT:
                if link is None:
                    set_state(SCRIBE_CLOSED)
                elif link.got_commit:
                    set_state(SCRIBE_HOLD_OPEN)
                    hold_secs_left = SCRIBE_HOLD_OPEN_SECS
                elif tail_secs_left > 0:
                    await send_chunk(chunk, silent=True)
                    tail_secs_left -= chunk_secs
                elif commit_wait_secs_left > 0:
                    commit_wait_secs_left -= chunk_secs
                else:
                    emit_event("scribe_commit_timeout")
                    await close_link("scribe_close")
                    set_state(SCRIBE_CLOSED)
                continue

            if state == SCRIBE_HOLD_OPEN:
                if link is None:
                    set_state(SCRIBE_CLOSED)
                else:
                    hold_secs_left -= chunk_secs
                    if hold_secs_left <= 0:
                        await close_link("scribe_close")
                        set_state(SCRIBE_CLOSED)
                continue

            # Quiet and not mid-utterance: settle into preopen (idle socket) or closed.
            if state == SCRIBE_RECONNECTING:
                set_state(SCRIBE_CLOSED)
            if link is not None and state == SCRIBE_CLOSED:
                set_state(SCRIBE_PREOPEN)
    finally:
        if open_task is not None:
            if not open_task.done():
                open_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                leftover = await open_task
                if leftover is not None and link is None:
                    link = leftover
        await close_link("scribe_close")
        if audio_levels is not None:
            audio_levels.scribe_gate_open = False
        set_state(SCRIBE_CLOSED)


async def speak_with_eleven_flash(
    text_chunks: AsyncIterator[str | VoiceSwitch],
    elevenlabs_api_key: str,
    voice_id: str,
    playback_event: asyncio.Event,
    speaking_event: asyncio.Event,
    audio_writer: Callable[[bytes], object] | None = None,
    on_playback_rms: Callable[[int], None] | None = None,
    profile_every: int = 0,
    usage: Any = None,
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
                        "generation_config": {"chunk_length_schedule": [50, 120, 250, 290]},
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
                await ws.send(json.dumps({"text": chunk}))
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

            if usage is not None:
                usage.tts_characters += len(chunk)
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
