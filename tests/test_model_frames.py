import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

import voice.model_frames as model_frames


class ModelFramesTest(unittest.TestCase):
    def test_save_writes_jpg_and_caption_sidecar(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(model_frames, "MODEL_FRAMES_DIR", Path(tmpdir)):
                model_frames.save_model_frame(b"jpeg-bytes", "look", "hello caption")

            jpgs = list(Path(tmpdir).glob("*.jpg"))
            self.assertEqual(len(jpgs), 1)
            self.assertEqual(jpgs[0].read_bytes(), b"jpeg-bytes")
            self.assertEqual(jpgs[0].with_suffix(".txt").read_text(encoding="utf-8"), "hello caption")

    def test_save_prunes_oldest_frames_and_sidecars(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            for index in range(model_frames.MAX_FRAMES + 3):
                stem = f"{index:013d}-look"
                (base / f"{stem}.jpg").write_bytes(b"j")
                (base / f"{stem}.txt").write_text("c", encoding="utf-8")

            with mock.patch.object(model_frames, "MODEL_FRAMES_DIR", base):
                model_frames.save_model_frame(b"new", "look")

            jpgs = sorted(base.glob("*.jpg"), key=lambda path: path.name)
            self.assertEqual(len(jpgs), model_frames.MAX_FRAMES)
            self.assertEqual(jpgs[-1].read_bytes(), b"new")
            self.assertFalse((base / "0000000000000-look.txt").exists())

    def test_unwritable_dir_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            blocked = Path(tmpdir) / "blocked"
            blocked.write_text("not a directory", encoding="utf-8")
            with mock.patch.object(model_frames, "MODEL_FRAMES_DIR", blocked):
                model_frames.save_model_frame(b"jpeg", "look")


if __name__ == "__main__":
    unittest.main()
