import array
import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from drivers.respeaker import ReSpeakerAudio, ReSpeakerError, extract_mono_channel


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


if __name__ == "__main__":
    unittest.main()
