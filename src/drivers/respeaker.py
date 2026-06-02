from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import suppress

import numpy as np

from lib.log import setup_logging

MIC_BLOCKSIZE = 1280
OUTPUT_BLOCKSIZE = 1600
PLAYBACK_QUEUE_MAXSIZE = 50
MIC_SUBSCRIBER_QUEUE_SIZE = 50
WAKE_MIC_QUEUE_SIZE = 10  # Phase A: mic_frames(..., queue_size=WAKE_MIC_QUEUE_SIZE, warn_on_drop=False)
CAPTURE_RAW_QUEUE_SIZE = 50
PLAYBACK_DRAIN_PADDING_SECS = 2.0
OVERFLOW_LOG_INTERVAL_SECS = 5.0
_PLAYBACK_STOP = object()

log = logging.getLogger(__name__)
profile_log = setup_logging("robot-voice")


class PlaybackPcmBuffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending = bytearray()

    def extend(self, audio: bytes) -> None:
        with self._lock:
            self._pending.extend(audio)

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()

    def pending_bytes(self) -> int:
        with self._lock:
            return len(self._pending)

    def fill(self, outdata) -> None:
        need = len(outdata)
        with self._lock:
            available = len(self._pending)
            if available >= need:
                outdata[:need] = self._pending[:need]
                del self._pending[:need]
            else:
                if available:
                    outdata[:available] = self._pending[:available]
                if available < need:
                    outdata[available:] = b"\x00" * (need - available)
                self._pending.clear()


class ReSpeakerError(RuntimeError):
    pass


def format_sounddevice_devices(kind: str) -> str:
    try:
        import sounddevice as sd
    except ModuleNotFoundError:
        return "sounddevice is not installed"

    rows = []
    for index, device in enumerate(sd.query_devices()):
        channels = device["max_input_channels"] if kind == "input" else device["max_output_channels"]
        if channels:
            rows.append(f"{index}: {device['name']}")
    return "; ".join(rows) if rows else f"no {kind} devices reported by PortAudio"


def sounddevice_selector(device: str) -> str:
    if device.startswith("plughw:"):
        return "hw:" + device.removeprefix("plughw:")
    return device


def extract_mono_channel(interleaved_pcm: bytes, channels: int, channel_index: int) -> bytes:
    if channel_index < 0 or channel_index >= channels:
        raise ReSpeakerError(f"channel_index must be between 0 and {channels - 1}")
    samples = memoryview(interleaved_pcm).cast("h")
    if len(samples) % channels != 0:
        raise ReSpeakerError("interleaved PCM does not contain whole frames")
    return samples[channel_index::channels].tobytes()


def apply_pcm16_gain(audio: bytes, gain: float) -> bytes:
    if gain == 1.0:
        return audio
    samples = np.frombuffer(audio, dtype=np.int16)
    return np.clip(samples * gain, -32768, 32767).astype(np.int16).tobytes()


class _MicSubscriber:
    __slots__ = ("queue", "warn_on_drop")

    def __init__(self, queue_size: int, warn_on_drop: bool) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_size)
        self.warn_on_drop = warn_on_drop


class ReSpeakerAudio:
    def __init__(
        self,
        input_device: str,
        output_device: str,
        sample_rate: int = 16000,
        capture_channels: int = 6,
        capture_channel_index: int = 1,
        output_channels: int = 1,
        input_gain: float = 1.0,
        output_gain: float = 1.0,
        profile_every: int = 0,
    ) -> None:
        if sample_rate != 16000:
            raise ReSpeakerError("sample_rate must be 16000")
        if capture_channels != 6:
            raise ReSpeakerError("capture_channels must be 6")
        if output_channels != 1:
            raise ReSpeakerError("output_channels must be 1")
        if capture_channel_index < 0 or capture_channel_index >= capture_channels:
            raise ReSpeakerError("capture_channel_index must be between 0 and 5")
        self.input_device = input_device
        self.output_device = output_device
        self.sample_rate = sample_rate
        self.capture_channels = capture_channels
        self.capture_channel_index = capture_channel_index
        self.output_channels = output_channels
        self.input_gain = input_gain
        self.output_gain = output_gain
        self.profile_every = profile_every
        self._playback_lock = asyncio.Lock()
        self._playback_id = 0
        self._active_playback_id: int | None = None
        self._playback_queue: asyncio.Queue[bytes | object] | None = None
        self._playback_task: asyncio.Task[None] | None = None
        self._io_stop_event: asyncio.Event | None = None
        self._capture_raw_queue: asyncio.Queue[bytes] | None = None
        self._capture_task: asyncio.Task[None] | None = None
        self._input_stream = None
        self._output_stream = None
        self._output_buffer = PlaybackPcmBuffer()
        self._subscribers: list[_MicSubscriber] = []
        self._last_capture_status_log = 0.0
        self._last_playback_status_log = 0.0
        self._last_drop_warn = 0.0
        self._last_raw_drop_log = 0.0
        self._capture_profile_count = 0

    async def start_io(self, stop_event: asyncio.Event) -> None:
        # One input + one output stream for the whole voice session (see robot_voice.py).
        # begin_playback/end_playback only queue PCM; they do not open/close PortAudio.
        # Chime (Phase A) and TTS must not overlap — both use _run_playback on this path.
        if self._capture_task is not None:
            raise ReSpeakerError("IO already started")
        import sounddevice as sd

        self._io_stop_event = stop_event
        loop = asyncio.get_running_loop()
        self._capture_raw_queue = asyncio.Queue(maxsize=CAPTURE_RAW_QUEUE_SIZE)

        def enqueue_raw(chunk: bytes) -> None:
            queue = self._capture_raw_queue
            if queue is None:
                return
            if queue.full():
                now = time.monotonic()
                if now - self._last_raw_drop_log >= OVERFLOW_LOG_INTERVAL_SECS:
                    self._last_raw_drop_log = now
                    log.warning("ReSpeaker raw capture queue full; dropping frame")
                return
            queue.put_nowait(chunk)

        def input_callback(indata, _frames, _time, status) -> None:
            if status:
                now = time.monotonic()
                if now - self._last_capture_status_log >= OVERFLOW_LOG_INTERVAL_SECS:
                    self._last_capture_status_log = now
                    log.warning("ReSpeaker capture status: %s", status)
            loop.call_soon_threadsafe(enqueue_raw, bytes(indata))

        def output_callback(outdata, _frames, _time, status) -> None:
            if status:
                now = time.monotonic()
                if now - self._last_playback_status_log >= OVERFLOW_LOG_INTERVAL_SECS:
                    self._last_playback_status_log = now
                    log.warning("ReSpeaker playback status: %s", status)
            self._output_buffer.fill(outdata)

        input_stream = None
        output_stream = None
        try:
            input_stream = sd.RawInputStream(
                device=sounddevice_selector(self.input_device),
                samplerate=self.sample_rate,
                blocksize=MIC_BLOCKSIZE,
                channels=self.capture_channels,
                dtype="int16",
                callback=input_callback,
            )
            output_stream = sd.RawOutputStream(
                device=sounddevice_selector(self.output_device),
                samplerate=self.sample_rate,
                channels=self.output_channels,
                dtype="int16",
                blocksize=OUTPUT_BLOCKSIZE,
                latency="high",
                callback=output_callback,
            )
            input_stream.start()
            output_stream.start()
        except Exception as exc:
            if input_stream is not None:
                input_stream.stop()
                input_stream.close()
            if output_stream is not None:
                output_stream.stop()
                output_stream.close()
            self._capture_raw_queue = None
            self._io_stop_event = None
            kind = "output" if input_stream is not None else "input"
            raise ReSpeakerError(f"{exc}; {kind} devices: {format_sounddevice_devices(kind)}") from exc

        self._input_stream = input_stream
        self._output_stream = output_stream
        self._capture_task = asyncio.create_task(self._capture_loop())

    async def stop_io(self) -> None:
        if self._capture_task is None:
            return
        self._capture_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._capture_task
        self._capture_task = None
        self._subscribers.clear()
        if self._input_stream is not None:
            self._input_stream.stop()
            self._input_stream.close()
            self._input_stream = None
        if self._output_stream is not None:
            self._output_stream.stop()
            self._output_stream.close()
            self._output_stream = None
        self._capture_raw_queue = None
        self._io_stop_event = None
        async with self._playback_lock:
            await self._finish_playback()
        self._output_buffer.clear()

    async def mic_frames(
        self,
        stop_event: asyncio.Event | None = None,
        *,
        queue_size: int = MIC_SUBSCRIBER_QUEUE_SIZE,
        warn_on_drop: bool = True,
    ) -> AsyncIterator[bytes]:
        if self._capture_task is None:
            raise ReSpeakerError("IO not started; call start_io() first")
        subscriber = _MicSubscriber(queue_size, warn_on_drop)
        self._subscribers.append(subscriber)
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                if self._io_stop_event is not None and self._io_stop_event.is_set():
                    break
                try:
                    frame = await asyncio.wait_for(subscriber.queue.get(), timeout=0.1)
                except TimeoutError:
                    continue
                yield frame
        finally:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    async def _capture_loop(self) -> None:
        stop_event = self._io_stop_event
        raw_queue = self._capture_raw_queue
        if stop_event is None or raw_queue is None:
            return
        try:
            while not stop_event.is_set():
                try:
                    interleaved = await asyncio.wait_for(raw_queue.get(), timeout=0.1)
                except TimeoutError:
                    continue
                started = time.perf_counter()
                mono_started = time.perf_counter()
                mono = extract_mono_channel(interleaved, self.capture_channels, self.capture_channel_index)
                mono_seconds = time.perf_counter() - mono_started
                gain_started = time.perf_counter()
                frame = apply_pcm16_gain(mono, self.input_gain)
                gain_seconds = time.perf_counter() - gain_started
                fanout_started = time.perf_counter()
                subscriber_count = len(self._subscribers)
                for subscriber in list(self._subscribers):
                    if subscriber.queue.full():
                        if subscriber.warn_on_drop:
                            now = time.monotonic()
                            if now - self._last_drop_warn >= OVERFLOW_LOG_INTERVAL_SECS:
                                self._last_drop_warn = now
                                log.warning("mic subscriber queue full; dropping frame")
                        continue
                    subscriber.queue.put_nowait(frame)
                fanout_seconds = time.perf_counter() - fanout_started
                self._maybe_log_capture_profile(
                    subscriber_count,
                    mono_seconds,
                    gain_seconds,
                    fanout_seconds,
                    time.perf_counter() - started,
                )
        except asyncio.CancelledError:
            raise

    def _maybe_log_capture_profile(
        self,
        subscriber_count: int,
        mono_seconds: float,
        gain_seconds: float,
        fanout_seconds: float,
        total_seconds: float,
    ) -> None:
        if self.profile_every <= 0:
            return
        self._capture_profile_count += 1
        if self._capture_profile_count % self.profile_every != 0:
            return
        profile_log.info(
            "voice audio profile: subscribers=%d mono=%.1fms gain=%.1fms fanout=%.1fms total=%.1fms",
            subscriber_count,
            mono_seconds * 1000,
            gain_seconds * 1000,
            fanout_seconds * 1000,
            total_seconds * 1000,
        )

    # Playback contract (see docs/plans/2026-05-24 - voice-core-stabilization.md, Phase 2):
    #   begin_playback()                  -> open a new output queue, return its playback_id
    #   write_output(pcm)                 -> queue PCM for the current playback
    #   end_playback(playback_id, drain)  -> finish that playback gracefully if it is still current;
    #                                        a stale id is a no-op so an old turn cannot stop a new one
    #   stop_playback_now()               -> synchronous, immediate: cancel the playback task and clear
    #                                        the hardware output buffer. Never awaits anything; safe to
    #                                        call from the scribe event loop without blocking input.
    # Voice turn callers:
    #   normal TTS completion  -> end_playback(id, drain=True)
    #   cancelled TTS cleanup  -> end_playback(id, drain=False)
    #   barge-in fast path     -> stop_playback_now()  (before any task cleanup)
    async def begin_playback(self) -> int:
        if self._output_stream is None:
            raise ReSpeakerError("IO not started; call start_io() first")
        async with self._playback_lock:
            await self._finish_playback()
            self._playback_id += 1
            playback_id = self._playback_id
            self._active_playback_id = playback_id
            self._playback_queue = asyncio.Queue(maxsize=PLAYBACK_QUEUE_MAXSIZE)
            self._playback_task = asyncio.create_task(self._run_playback())
            return playback_id

    async def write_output(self, audio: bytes) -> None:
        if self._playback_queue is None:
            raise ReSpeakerError("playback not started; call begin_playback() first")
        await self._playback_queue.put(audio)

    async def end_playback(self, playback_id: int | None = None, *, drain: bool = True) -> None:
        async with self._playback_lock:
            if playback_id is not None and playback_id != self._active_playback_id:
                return
            await self._finish_playback(drain=drain)

    def stop_playback_now(self) -> None:
        if self._playback_task is not None:
            self._playback_task.cancel()
            self._playback_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        self._playback_queue = None
        self._playback_task = None
        self._active_playback_id = None
        self._output_buffer.clear()

    async def play_wav(self, path: str) -> None:
        import wave

        if self._output_stream is None:
            raise ReSpeakerError("IO not started; call start_io() first")
        with wave.open(path, "rb") as wav_file:
            if wav_file.getnchannels() != 1:
                raise ReSpeakerError("WAV must be mono")
            if wav_file.getsampwidth() != 2:
                raise ReSpeakerError("WAV must be 16-bit PCM")
            if wav_file.getframerate() != self.sample_rate:
                raise ReSpeakerError(f"WAV must be {self.sample_rate} Hz")
            pcm = wav_file.readframes(wav_file.getnframes())
        playback_id = await self.begin_playback()
        try:
            await self.write_output(pcm)
        finally:
            await self.end_playback(playback_id)

    async def _finish_playback(self, *, drain: bool = True) -> None:
        if self._playback_task is None or self._playback_queue is None:
            return
        try:
            if drain:
                await self._playback_queue.put(_PLAYBACK_STOP)
                await self._playback_task
            else:
                self._playback_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._playback_task
                self._output_buffer.clear()
        finally:
            self._playback_queue = None
            self._playback_task = None
            self._active_playback_id = None

    async def _drain_playback_buffer(self) -> None:
        buffer = self._output_buffer
        pending = buffer.pending_bytes()
        if pending == 0:
            return
        bytes_per_second = self.sample_rate * self.output_channels * 2
        timeout = pending / bytes_per_second + PLAYBACK_DRAIN_PADDING_SECS
        deadline = asyncio.get_running_loop().time() + timeout
        while buffer.pending_bytes() > 0:
            if asyncio.get_running_loop().time() >= deadline:
                log.warning("playback drain timed out; clearing %d pending bytes", buffer.pending_bytes())
                break
            await asyncio.sleep(OUTPUT_BLOCKSIZE / self.sample_rate / 4)
        buffer.clear()

    async def _run_playback(self) -> None:
        while self._playback_queue is not None:
            try:
                audio = await asyncio.wait_for(self._playback_queue.get(), timeout=0.05)
            except TimeoutError:
                continue
            if audio is _PLAYBACK_STOP:
                await self._drain_playback_buffer()
                return
            self._output_buffer.extend(apply_pcm16_gain(audio, self.output_gain))
            pump = getattr(self._output_stream, "pump", None)
            if pump is not None:
                pump(self._output_buffer)
