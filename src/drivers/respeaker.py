from __future__ import annotations

import asyncio
import array
import logging
import threading
from collections.abc import AsyncIterator


log = logging.getLogger("robot-voice")


MIC_BLOCKSIZE = 3200
OUTPUT_BLOCKSIZE = 1600
PLAYBACK_QUEUE_MAXSIZE = 50
_PLAYBACK_STOP = object()


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
    samples = array.array("h")
    samples.frombytes(audio)
    for index, sample in enumerate(samples):
        samples[index] = max(-32768, min(32767, int(sample * gain)))
    return samples.tobytes()


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
        self._playback_lock = asyncio.Lock()
        self._playback_id = 0
        self._active_playback_id: int | None = None
        self._playback_queue: asyncio.Queue[bytes | object] | None = None
        self._playback_task: asyncio.Task[None] | None = None

    async def microphone_chunks(self, stop_event: asyncio.Event) -> AsyncIterator[bytes]:
        import sounddevice as sd

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)

        def enqueue(chunk: bytes) -> None:
            if not queue.full():
                queue.put_nowait(chunk)

        def callback(indata, _frames, _time, status):
            if not status:
                loop.call_soon_threadsafe(enqueue, bytes(indata))

        try:
            stream = sd.RawInputStream(
                device=sounddevice_selector(self.input_device),
                samplerate=self.sample_rate,
                blocksize=MIC_BLOCKSIZE,
                channels=self.capture_channels,
                dtype="int16",
                callback=callback,
            )
        except Exception as exc:
            raise ReSpeakerError(f"{exc}; input devices: {format_sounddevice_devices('input')}") from exc

        with stream:
            while not stop_event.is_set():
                interleaved = await queue.get()
                mono = extract_mono_channel(interleaved, self.capture_channels, self.capture_channel_index)
                yield apply_pcm16_gain(mono, self.input_gain)

    async def begin_playback(self) -> int:
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

    async def end_playback(self, playback_id: int | None = None) -> None:
        async with self._playback_lock:
            if playback_id is not None and playback_id != self._active_playback_id:
                return
            await self._finish_playback()

    async def _finish_playback(self) -> None:
        if self._playback_task is None or self._playback_queue is None:
            return
        try:
            await self._playback_queue.put(_PLAYBACK_STOP)
            await self._playback_task
        finally:
            self._playback_queue = None
            self._playback_task = None
            self._active_playback_id = None

    async def _run_playback(self) -> None:
        import sounddevice as sd

        buffer = PlaybackPcmBuffer()
        restart = threading.Event()
        diag = {"chunks": 0, "bytes": 0, "status_hits": 0}

        def callback(outdata, frames, _time, status) -> None:
            if status:
                diag["status_hits"] += 1
                if diag["status_hits"] <= 3:
                    log.info("playback diag: callback status=%s pending=%s", status, buffer.pending_bytes())
                restart.set()
            buffer.fill(outdata)

        while self._playback_queue is not None:
            restart.clear()
            diag = {"chunks": 0, "bytes": 0, "status_hits": 0}
            try:
                stream = sd.RawOutputStream(
                    device=sounddevice_selector(self.output_device),
                    samplerate=self.sample_rate,
                    channels=self.output_channels,
                    dtype="int16",
                    blocksize=OUTPUT_BLOCKSIZE,
                    latency="high",
                    callback=callback,
                )
            except Exception as exc:
                raise ReSpeakerError(f"{exc}; output devices: {format_sounddevice_devices('output')}") from exc

            with stream:
                if restart.is_set():
                    log.info("playback diag: restart set before inner loop (status_hits=%s)", diag["status_hits"])
                while self._playback_queue is not None and not restart.is_set():
                    try:
                        audio = await asyncio.wait_for(self._playback_queue.get(), timeout=0.05)
                    except TimeoutError:
                        continue
                    if audio is _PLAYBACK_STOP:
                        return
                    audio = apply_pcm16_gain(audio, self.output_gain)
                    buffer.extend(audio)
                    diag["chunks"] += 1
                    diag["bytes"] += len(audio)
                    pump = getattr(stream, "pump", None)
                    if pump is not None:
                        pump(buffer)
            log.info(
                "playback diag: stream closed chunks=%s bytes=%s status_hits=%s restart=%s buf_pending=%s",
                diag["chunks"],
                diag["bytes"],
                diag["status_hits"],
                restart.is_set(),
                buffer.pending_bytes(),
            )
