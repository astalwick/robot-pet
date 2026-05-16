from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator


MIC_BLOCKSIZE = 3200
OUTPUT_WRITE_CHUNK_MS = 50


class ReSpeakerError(RuntimeError):
    pass


def extract_mono_channel(interleaved_pcm: bytes, channels: int, channel_index: int) -> bytes:
    if channel_index < 0 or channel_index >= channels:
        raise ReSpeakerError(f"channel_index must be between 0 and {channels - 1}")
    samples = memoryview(interleaved_pcm).cast("h")
    if len(samples) % channels != 0:
        raise ReSpeakerError("interleaved PCM does not contain whole frames")
    return samples[channel_index::channels].tobytes()


class ReSpeakerAudio:
    def __init__(
        self,
        input_device: str,
        output_device: str,
        sample_rate: int = 16000,
        capture_channels: int = 6,
        capture_channel_index: int = 1,
        output_channels: int = 1,
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

        with sd.RawInputStream(
            device=self.input_device,
            samplerate=self.sample_rate,
            blocksize=MIC_BLOCKSIZE,
            channels=self.capture_channels,
            dtype="int16",
            callback=callback,
        ):
            while not stop_event.is_set():
                interleaved = await queue.get()
                yield extract_mono_channel(interleaved, self.capture_channels, self.capture_channel_index)

    async def write_output(self, audio: bytes) -> None:
        import sounddevice as sd

        chunk_bytes = self.sample_rate * self.output_channels * 2 * OUTPUT_WRITE_CHUNK_MS // 1000
        with sd.RawOutputStream(
            device=self.output_device,
            samplerate=self.sample_rate,
            channels=self.output_channels,
            dtype="int16",
            blocksize=0,
        ) as output:
            for index in range(0, len(audio), chunk_bytes):
                await asyncio.to_thread(output.write, audio[index : index + chunk_bytes])
