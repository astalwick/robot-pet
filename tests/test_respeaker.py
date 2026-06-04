import array
import asyncio
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from drivers.respeaker import (
    DoAReading,
    MIC_BLOCKSIZE,
    MIC_SUBSCRIBER_QUEUE_SIZE,
    PlaybackPcmBuffer,
    ReSpeakerAudio,
    ReSpeakerDoA,
    ReSpeakerError,
    apply_pcm16_gain,
    extract_mono_channel,
    format_sounddevice_devices,
    parse_doa_response,
    sounddevice_selector,
)


def pcm16(values):
    samples = array.array("h", values)
    return samples.tobytes()


class ReSpeakerTest(unittest.TestCase):
    def test_parses_doa_response(self):
        self.assertEqual(parse_doa_response([0, 14, 1, 1, 0]), DoAReading(270, True))
        self.assertEqual(parse_doa_response([0, 100, 1, 0, 0]), DoAReading(356, False))

    def test_rejects_invalid_doa_response(self):
        with self.assertRaisesRegex(ReSpeakerError, "Malformed"):
            parse_doa_response([0, 14, 1])
        with self.assertRaisesRegex(ReSpeakerError, "Invalid"):
            parse_doa_response([0, 104, 1, 1, 0])

    def test_reads_doa_from_usb_control_device(self):
        device = mock.Mock()
        device.ctrl_transfer.return_value = [0, 15, 1, 1, 0]

        reading = ReSpeakerDoA(device).read()

        self.assertEqual(reading, DoAReading(271, True))
        device.ctrl_transfer.assert_called_once_with(0xC0, 0, 0x80 | 18, 20, 5, 1000)

    def test_reports_doa_usb_read_failure(self):
        device = mock.Mock()
        device.ctrl_transfer.side_effect = OSError("USB disconnected")

        with self.assertRaisesRegex(ReSpeakerError, "USB disconnected"):
            ReSpeakerDoA(device).read()

    def test_opens_respeaker_usb_control_device(self):
        device = object()
        usb = types.ModuleType("usb")
        usb.core = types.ModuleType("usb.core")
        usb.core.find = mock.Mock(return_value=device)

        with mock.patch.dict(sys.modules, {"usb": usb, "usb.core": usb.core}):
            doa = ReSpeakerDoA.open()

        self.assertIs(doa.device, device)
        usb.core.find.assert_called_once_with(idVendor=0x2886, idProduct=0x001E)

        usb.core.find.return_value = None
        with mock.patch.dict(sys.modules, {"usb": usb, "usb.core": usb.core}):
            with self.assertRaisesRegex(ReSpeakerError, "not found"):
                ReSpeakerDoA.open()

    def test_extracts_channel_zero(self):
        interleaved = pcm16([10, 11, 12, 13, 14, 15, 20, 21, 22, 23, 24, 25])

        self.assertEqual(extract_mono_channel(interleaved, 6, 0), pcm16([10, 20]))

    def test_extracts_channel_one(self):
        interleaved = pcm16([10, 11, 12, 13, 14, 15, 20, 21, 22, 23, 24, 25])

        self.assertEqual(extract_mono_channel(interleaved, 6, 1), pcm16([11, 21]))

    def test_extracts_channel_five(self):
        interleaved = pcm16([10, 11, 12, 13, 14, 15, 20, 21, 22, 23, 24, 25])

        self.assertEqual(extract_mono_channel(interleaved, 6, 5), pcm16([15, 25]))

    def test_invalid_channel_index_fails_early(self):
        with self.assertRaisesRegex(ReSpeakerError, "channel_index"):
            extract_mono_channel(pcm16([1, 2, 3, 4, 5, 6]), 6, 6)

    def test_output_writer_can_be_faked(self):
        writes = []

        class FakeAudio(ReSpeakerAudio):
            async def write_output(self, audio: bytes) -> None:
                writes.append(audio)

        audio = FakeAudio("hw:0,0", "plughw:0,0")
        asyncio.run(audio.write_output(b"abc"))

        self.assertEqual(writes, [b"abc"])

    def test_playback_buffer_fills_and_drains(self):
        buffer = PlaybackPcmBuffer()
        buffer.extend(b"abcd")
        outdata = bytearray(8)
        buffer.fill(outdata)
        self.assertEqual(bytes(outdata), b"abcd\x00\x00\x00\x00")
        buffer.extend(b"ef")
        outdata = bytearray(4)
        buffer.fill(outdata)
        self.assertEqual(bytes(outdata), b"ef\x00\x00")

    def test_mic_blocksize_is_80ms_at_16khz(self):
        self.assertEqual(MIC_BLOCKSIZE, 1280)

    def test_playback_reuses_one_output_stream(self):
        class FakeStream:
            opened = 0
            writes: list[bytes] = []

            def __init__(self, callback=None, blocksize=4, **_kwargs):
                FakeStream.opened += 1
                self._callback = callback
                self._blocksize = blocksize

            def start(self):
                return None

            def stop(self):
                return None

            def close(self):
                return None

            def pump(self, buffer):
                if not self._callback:
                    return
                outdata = bytearray(self._blocksize * 2)
                self._callback(outdata, self._blocksize, None, None)
                FakeStream.writes.append(bytes(outdata))

        class FakeSoundDevice:
            RawInputStream = FakeStream
            RawOutputStream = FakeStream

        async def run():
            FakeStream.opened = 0
            FakeStream.writes = []
            sys.modules["sounddevice"] = FakeSoundDevice
            audio = ReSpeakerAudio("hw:0,0", "plughw:0,0")
            stop_event = asyncio.Event()
            await audio.start_io(stop_event)
            await audio.begin_playback()
            await audio.write_output(b"abc")
            await audio.write_output(b"def")
            await audio.end_playback()
            await audio.begin_playback()
            await audio.write_output(b"ghi")
            await audio.end_playback()
            await audio.stop_io()

            self.assertEqual(FakeStream.opened, 2)
            played = b"".join(FakeStream.writes)
            self.assertIn(b"abc", played)
            self.assertIn(b"def", played)
            self.assertIn(b"ghi", played)

        try:
            asyncio.run(run())
        finally:
            del sys.modules["sounddevice"]

    def test_write_output_without_begin_fails(self):
        audio = ReSpeakerAudio("hw:0,0", "plughw:0,0")

        with self.assertRaisesRegex(ReSpeakerError, "playback not started"):
            asyncio.run(audio.write_output(b"abc"))

    def test_begin_playback_without_start_io_fails(self):
        audio = ReSpeakerAudio("hw:0,0", "plughw:0,0")

        with self.assertRaisesRegex(ReSpeakerError, "IO not started"):
            asyncio.run(audio.begin_playback())

    def test_start_io_reports_missing_output_device(self):
        class FailOutputStream:
            def __init__(self, **_kwargs):
                raise OSError("output unavailable")

        class FakeInputStream:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                return None

            def stop(self):
                return None

            def close(self):
                return None

        class FakeSoundDevice:
            RawInputStream = FakeInputStream
            RawOutputStream = FailOutputStream

            @staticmethod
            def query_devices():
                return []

        async def run():
            sys.modules["sounddevice"] = FakeSoundDevice
            audio = ReSpeakerAudio("hw:0,0", "plughw:0,0")
            with self.assertRaises(ReSpeakerError):
                await audio.start_io(asyncio.Event())

        try:
            asyncio.run(run())
        finally:
            del sys.modules["sounddevice"]

    def test_stale_end_playback_does_not_stop_new_session(self):
        class FakeStream:
            writes: list[bytes] = []

            def __init__(self, callback=None, blocksize=4, **_kwargs):
                self._callback = callback
                self._blocksize = blocksize

            def start(self):
                return None

            def stop(self):
                return None

            def close(self):
                return None

            def pump(self, buffer):
                if not self._callback:
                    return
                outdata = bytearray(self._blocksize * 2)
                self._callback(outdata, self._blocksize, None, None)
                FakeStream.writes.append(bytes(outdata))

        class FakeSoundDevice:
            RawInputStream = FakeStream
            RawOutputStream = FakeStream

            @staticmethod
            def query_devices():
                return []

        async def run():
            FakeStream.writes = []
            sys.modules["sounddevice"] = FakeSoundDevice
            audio = ReSpeakerAudio("hw:0,0", "plughw:0,0")
            await audio.start_io(asyncio.Event())
            old_id = await audio.begin_playback()
            await audio.begin_playback()
            await audio.end_playback(old_id)
            await audio.write_output(b"still playing")
            await audio.end_playback()

            self.assertIn(b"still playing", b"".join(FakeStream.writes))

        try:
            asyncio.run(run())
        finally:
            del sys.modules["sounddevice"]

    def test_end_playback_without_drain_flushes_buffer(self):
        class FakeStream:
            def __init__(self, callback=None, blocksize=4, **_kwargs):
                self._callback = callback
                self._blocksize = blocksize

            def start(self):
                return None

            def stop(self):
                return None

            def close(self):
                return None

            def pump(self, _buffer):
                return None

        class FakeSoundDevice:
            RawInputStream = FakeStream
            RawOutputStream = FakeStream

            @staticmethod
            def query_devices():
                return []

        async def run():
            sys.modules["sounddevice"] = FakeSoundDevice
            audio = ReSpeakerAudio("hw:0,0", "plughw:0,0")
            await audio.start_io(asyncio.Event())
            playback_id = await audio.begin_playback()
            await audio.write_output(b"still pending")
            for _ in range(10):
                if audio._output_buffer.pending_bytes() > 0:
                    break
                await asyncio.sleep(0.01)

            self.assertGreater(audio._output_buffer.pending_bytes(), 0)

            await audio.end_playback(playback_id, drain=False)

            self.assertEqual(audio._output_buffer.pending_bytes(), 0)

        try:
            asyncio.run(run())
        finally:
            del sys.modules["sounddevice"]

    def test_stop_playback_now_is_synchronous_and_clears_buffer(self):
        # Phase 2 acceptance: stop_playback_now() is the barge-in fast path.
        # It must not await anything (so the scribe event loop never blocks),
        # it must drop pending PCM, and it must release the active playback id
        # so a stale end_playback(old_id) is a no-op.
        class FakeStream:
            def __init__(self, callback=None, blocksize=4, **_kwargs):
                self._callback = callback
                self._blocksize = blocksize

            def start(self):
                return None

            def stop(self):
                return None

            def close(self):
                return None

            def pump(self, _buffer):
                return None

        class FakeSoundDevice:
            RawInputStream = FakeStream
            RawOutputStream = FakeStream

            @staticmethod
            def query_devices():
                return []

        async def run():
            sys.modules["sounddevice"] = FakeSoundDevice
            audio = ReSpeakerAudio("hw:0,0", "plughw:0,0")
            await audio.start_io(asyncio.Event())
            playback_id = await audio.begin_playback()
            await audio.write_output(b"about to be dropped")
            for _ in range(10):
                if audio._output_buffer.pending_bytes() > 0:
                    break
                await asyncio.sleep(0.01)
            self.assertGreater(audio._output_buffer.pending_bytes(), 0)

            self.assertIsNone(audio.stop_playback_now())
            self.assertEqual(audio._output_buffer.pending_bytes(), 0)
            self.assertIsNone(audio._playback_queue)
            self.assertIsNone(audio._active_playback_id)

            await audio.end_playback(playback_id)

        try:
            asyncio.run(run())
        finally:
            del sys.modules["sounddevice"]

    def test_begin_playback_waits_for_in_flight_end(self):
        class FakeStream:
            writes: list[bytes] = []

            def __init__(self, callback=None, blocksize=4, **_kwargs):
                self._callback = callback
                self._blocksize = blocksize

            def start(self):
                return None

            def stop(self):
                return None

            def close(self):
                return None

            def pump(self, buffer):
                if not self._callback:
                    return
                outdata = bytearray(self._blocksize * 2)
                self._callback(outdata, self._blocksize, None, None)
                FakeStream.writes.append(bytes(outdata))

        class FakeSoundDevice:
            RawInputStream = FakeStream
            RawOutputStream = FakeStream

            @staticmethod
            def query_devices():
                return []

        async def run():
            FakeStream.writes = []
            sys.modules["sounddevice"] = FakeSoundDevice
            audio = ReSpeakerAudio("hw:0,0", "plughw:0,0")
            await audio.start_io(asyncio.Event())
            ending = asyncio.Event()

            async def end_first_session():
                ending.set()
                await audio.end_playback()

            await audio.begin_playback()
            end_task = asyncio.create_task(end_first_session())
            await ending.wait()
            await audio.begin_playback()
            await audio.write_output(b"next")
            await end_task
            await audio.end_playback()

            self.assertIn(b"next", b"".join(FakeStream.writes))

        try:
            asyncio.run(run())
        finally:
            del sys.modules["sounddevice"]

    def test_mic_frames_fan_out_to_subscribers(self):
        class FakeInputStream:
            callback = None

            def __init__(self, callback=None, **_kwargs):
                FakeInputStream.callback = callback

            def start(self):
                return None

            def stop(self):
                return None

            def close(self):
                return None

        class FakeOutputStream:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                return None

            def stop(self):
                return None

            def close(self):
                return None

        class FakeSoundDevice:
            RawInputStream = FakeInputStream
            RawOutputStream = FakeOutputStream

        interleaved = pcm16([10, 11, 12, 13, 14, 15])

        async def collect_frames(audio):
            frames = []
            async for frame in audio.mic_frames():
                frames.append(frame)
                if len(frames) >= 2:
                    break
            return frames

        async def run():
            sys.modules["sounddevice"] = FakeSoundDevice
            audio = ReSpeakerAudio("hw:0,0", "plughw:0,0", capture_channel_index=0)
            stop_event = asyncio.Event()
            await audio.start_io(stop_event)
            first = asyncio.create_task(collect_frames(audio))
            second = asyncio.create_task(collect_frames(audio))
            await asyncio.sleep(0.05)
            FakeInputStream.callback(interleaved, MIC_BLOCKSIZE, None, None)
            FakeInputStream.callback(interleaved, MIC_BLOCKSIZE, None, None)
            first_frames = await first
            second_frames = await second
            await audio.stop_io()

            self.assertEqual(first_frames, second_frames)
            self.assertEqual(first_frames[0], pcm16([10]))
            self.assertEqual(first_frames[1], pcm16([10]))

        try:
            asyncio.run(run())
        finally:
            del sys.modules["sounddevice"]

    def test_mic_frames_drops_when_queue_full(self):
        import logging
        from contextlib import suppress

        class FakeInputStream:
            callback = None

            def __init__(self, callback=None, **_kwargs):
                FakeInputStream.callback = callback

            def start(self):
                return None

            def stop(self):
                return None

            def close(self):
                return None

        class FakeOutputStream:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                return None

            def stop(self):
                return None

            def close(self):
                return None

        class FakeSoundDevice:
            RawInputStream = FakeInputStream
            RawOutputStream = FakeOutputStream

        interleaved = pcm16([1, 2, 3, 4, 5, 6])

        async def run():
            sys.modules["sounddevice"] = FakeSoundDevice
            audio = ReSpeakerAudio("hw:0,0", "plughw:0,0", capture_channel_index=0)
            await audio.start_io(asyncio.Event())

            first_seen = asyncio.Event()
            seen: list[bytes] = []

            async def stalled_consumer():
                # Hold the first frame so the subscriber queue can fill past its bound.
                async for frame in audio.mic_frames():
                    seen.append(frame)
                    first_seen.set()
                    await asyncio.sleep(60)

            consumer = asyncio.create_task(stalled_consumer())

            # Push frames in batches with yields so the capture loop drains
            # the raw queue between batches and fan-out hits the full subscriber.
            for _ in range(4):
                for _ in range(MIC_SUBSCRIBER_QUEUE_SIZE):
                    FakeInputStream.callback(interleaved, MIC_BLOCKSIZE, None, None)
                await asyncio.sleep(0.05)

            await first_seen.wait()
            consumer.cancel()
            with suppress(asyncio.CancelledError):
                await consumer
            await audio.stop_io()

            self.assertEqual(len(seen), 1)

        with self.assertLogs("drivers.respeaker", level=logging.WARNING) as captured:
            try:
                asyncio.run(run())
            finally:
                del sys.modules["sounddevice"]

        self.assertTrue(
            any("mic subscriber queue full" in message for message in captured.output),
            f"expected drop warning, got: {captured.output}",
        )

    def test_device_formatter_lists_matching_direction(self):
        class FakeSoundDevice:
            @staticmethod
            def query_devices():
                return [
                    {"name": "Mic Device", "max_input_channels": 6, "max_output_channels": 0},
                    {"name": "Speaker Device", "max_input_channels": 0, "max_output_channels": 2},
                ]

        sys.modules["sounddevice"] = FakeSoundDevice
        try:
            self.assertIn("Speaker Device", format_sounddevice_devices("output"))
            self.assertNotIn("Mic Device", format_sounddevice_devices("output"))
        finally:
            del sys.modules["sounddevice"]

    def test_plughw_config_maps_to_portaudio_hw_selector(self):
        self.assertEqual(sounddevice_selector("plughw:0,0"), "hw:0,0")
        self.assertEqual(sounddevice_selector("hw:0,0"), "hw:0,0")

    def test_pcm_gain_scales_samples(self):
        self.assertEqual(apply_pcm16_gain(pcm16([1000, -1000]), 0.5), pcm16([500, -500]))
        self.assertEqual(apply_pcm16_gain(pcm16([1000, -1000]), 2.0), pcm16([2000, -2000]))

    def test_pcm_gain_clips_to_int16(self):
        self.assertEqual(apply_pcm16_gain(pcm16([30000, -30000]), 2.0), pcm16([32767, -32768]))


if __name__ == "__main__":
    unittest.main()
