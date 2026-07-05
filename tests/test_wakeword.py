import array
import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from drivers.respeaker import MIC_BLOCKSIZE
from voice.wakeword import WakeWordDetector


def pcm16(values):
    samples = array.array("h", values)
    return samples.tobytes()


class FakeWakeModel:
    def __init__(self, score: float) -> None:
        self.score = score
        self.predict_calls = 0

    def predict(self, _frame):
        self.predict_calls += 1
        return {"hey_bloop": self.score}


class WakeWordDetectorTest(unittest.TestCase):
    def make_detector(self, score: float, threshold: float = 0.5, debounce_secs: float = 2.0) -> WakeWordDetector:
        detector = WakeWordDetector("/tmp/fake.onnx", threshold=threshold, debounce_secs=debounce_secs)
        detector._model = FakeWakeModel(score)
        detector._wake_name = "hey_bloop"
        return detector

    def test_check_fires_when_score_meets_threshold(self):
        detector = self.make_detector(0.8)
        frame = pcm16([0] * MIC_BLOCKSIZE)

        self.assertTrue(detector.check(frame, now=10.0))
        self.assertEqual(detector.fire_count, 1)
        self.assertEqual(detector.last_score, 0.8)

    def test_check_respects_debounce(self):
        detector = self.make_detector(0.8, debounce_secs=2.0)
        frame = pcm16([0] * MIC_BLOCKSIZE)

        self.assertTrue(detector.check(frame, now=10.0))
        self.assertFalse(detector.check(frame, now=11.0))
        self.assertTrue(detector.check(frame, now=12.0))

    def test_wrong_frame_size_scores_zero(self):
        detector = self.make_detector(0.9)

        self.assertEqual(detector.score(pcm16([0] * 64)), 0.0)

    def test_rms_gate_skips_predict_on_quiet_frames(self):
        detector = self.make_detector(0.8, threshold=0.5)
        detector.rms_gate_min = 50
        silent = pcm16([0] * MIC_BLOCKSIZE)
        loud = pcm16([1000] * MIC_BLOCKSIZE)

        self.assertFalse(detector.check(silent, now=10.0))
        self.assertEqual(detector._model.predict_calls, 0)
        self.assertEqual(detector.last_rms, 0)

        self.assertTrue(detector.check(loud, now=10.0))
        self.assertEqual(detector._model.predict_calls, 1)

    def test_rms_gate_zero_disables_gate(self):
        detector = self.make_detector(0.8, threshold=0.5)
        detector.rms_gate_min = 0
        silent = pcm16([0] * MIC_BLOCKSIZE)

        self.assertTrue(detector.check(silent, now=10.0))
        self.assertEqual(detector._model.predict_calls, 1)


class WakeChimePlaybackTest(unittest.IsolatedAsyncioTestCase):
    async def test_play_wav_pushes_pcm_through_playback(self):
        import tempfile
        import wave

        from drivers.respeaker import ReSpeakerAudio

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

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = tmp.name
        with wave.open(wav_path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm16([100, -100, 200, -200]))

        try:
            FakeStream.writes = []
            sys.modules["sounddevice"] = FakeSoundDevice
            audio = ReSpeakerAudio("hw:0,0", "plughw:0,0")
            await audio.start_io(asyncio.Event())
            await audio.play_wav(wav_path)
            await audio.stop_io()

            played = b"".join(FakeStream.writes)
            self.assertIn(pcm16([100, -100, 200, -200]), played)
        finally:
            del sys.modules["sounddevice"]
            os.unlink(wav_path)


if __name__ == "__main__":
    unittest.main()
