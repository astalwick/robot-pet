import array
import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from drivers.respeaker import (
    ReSpeakerAudio,
    ReSpeakerError,
    apply_pcm16_gain,
    extract_mono_channel,
    format_sounddevice_devices,
    sounddevice_selector,
)


def pcm16(values):
    samples = array.array("h", values)
    return samples.tobytes()


class ReSpeakerTest(unittest.TestCase):
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

    def test_playback_reuses_one_output_stream(self):
        class FakeOutputStream:
            opened = 0
            writes: list[bytes] = []

            def __init__(self, **_kwargs):
                FakeOutputStream.opened += 1

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def write(self, audio: bytes) -> None:
                FakeOutputStream.writes.append(audio)

        class FakeSoundDevice:
            RawOutputStream = FakeOutputStream

        async def run():
            FakeOutputStream.opened = 0
            FakeOutputStream.writes = []
            sys.modules["sounddevice"] = FakeSoundDevice
            audio = ReSpeakerAudio("hw:0,0", "plughw:0,0")
            await audio.begin_playback()
            await audio.write_output(b"abc")
            await audio.write_output(b"def")
            await audio.end_playback()

            self.assertEqual(FakeOutputStream.opened, 1)
            self.assertEqual(FakeOutputStream.writes, [b"abc", b"def"])

        try:
            asyncio.run(run())
        finally:
            del sys.modules["sounddevice"]

    def test_write_output_without_begin_fails(self):
        audio = ReSpeakerAudio("hw:0,0", "plughw:0,0")

        with self.assertRaisesRegex(ReSpeakerError, "playback not started"):
            asyncio.run(audio.write_output(b"abc"))

    def test_playback_recovers_after_stream_open_failure(self):
        class FailOutputStream:
            def __init__(self, **_kwargs):
                raise OSError("output unavailable")

        class GoodOutputStream:
            writes: list[bytes] = []

            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def write(self, audio: bytes) -> None:
                GoodOutputStream.writes.append(audio)

        class FakeSoundDevice:
            RawOutputStream = FailOutputStream

            @staticmethod
            def query_devices():
                return []

        async def run():
            GoodOutputStream.writes = []
            sys.modules["sounddevice"] = FakeSoundDevice
            audio = ReSpeakerAudio("hw:0,0", "plughw:0,0")
            await audio.begin_playback()
            with self.assertRaises(ReSpeakerError):
                await audio.end_playback()

            FakeSoundDevice.RawOutputStream = GoodOutputStream
            await audio.begin_playback()
            await audio.write_output(b"ok")
            await audio.end_playback()

            self.assertEqual(GoodOutputStream.writes, [b"ok"])

        try:
            asyncio.run(run())
        finally:
            del sys.modules["sounddevice"]

    def test_stale_end_playback_does_not_stop_new_session(self):
        class FakeOutputStream:
            writes: list[bytes] = []

            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def write(self, audio: bytes) -> None:
                FakeOutputStream.writes.append(audio)

        class FakeSoundDevice:
            RawOutputStream = FakeOutputStream

            @staticmethod
            def query_devices():
                return []

        async def run():
            FakeOutputStream.writes = []
            sys.modules["sounddevice"] = FakeSoundDevice
            audio = ReSpeakerAudio("hw:0,0", "plughw:0,0")
            old_id = await audio.begin_playback()
            await audio.begin_playback()
            await audio.end_playback(old_id)
            await audio.write_output(b"still playing")
            await audio.end_playback()

            self.assertEqual(FakeOutputStream.writes, [b"still playing"])

        try:
            asyncio.run(run())
        finally:
            del sys.modules["sounddevice"]

    def test_begin_playback_waits_for_in_flight_end(self):
        class FakeOutputStream:
            opened = 0
            writes: list[bytes] = []

            def __init__(self, **_kwargs):
                FakeOutputStream.opened += 1

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def write(self, audio: bytes) -> None:
                FakeOutputStream.writes.append(audio)

        class FakeSoundDevice:
            RawOutputStream = FakeOutputStream

            @staticmethod
            def query_devices():
                return []

        async def run():
            FakeOutputStream.opened = 0
            FakeOutputStream.writes = []
            sys.modules["sounddevice"] = FakeSoundDevice
            audio = ReSpeakerAudio("hw:0,0", "plughw:0,0")
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

            self.assertEqual(FakeOutputStream.opened, 2)
            self.assertEqual(FakeOutputStream.writes, [b"next"])

        try:
            asyncio.run(run())
        finally:
            del sys.modules["sounddevice"]

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
