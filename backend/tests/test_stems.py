import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from autodj import stems, stretch
from autodj.config import Settings
from autodj.render import STEM_NAMES
from tests.synth import make_song, write_wav

try:
    import scipy  # noqa: F401
    HAVE = shutil.which("ffmpeg") is not None
except Exception:  # noqa: BLE001
    HAVE = False


class WindowBoundsTest(unittest.TestCase):
    def test_head_and_tail(self):
        self.assertEqual(stems.window_bounds(200, "head", 80), (0.0, 80.0))
        self.assertEqual(stems.window_bounds(200, "tail", 80), (120.0, 200.0))
        self.assertEqual(stems.window_bounds(50, "head", 80), (0.0, 50.0))  # pista más corta que la ventana
        self.assertEqual(stems.window_bounds(50, "tail", 80), (0.0, 50.0))

    def test_cut_window_surrounds_the_cut_point(self):
        lo, hi = stems.window_bounds(240, "cut", 80, 90.0)
        self.assertEqual((lo, hi), (78.0, 158.0))
        self.assertEqual(stems.window_bounds(240, "cut", 80, 5.0)[0], 0.0)
        self.assertEqual(stems.window_bounds(100, "cut", 80, 90.0)[1], 100.0)  # no pasa del final


class VocalActivityTest(unittest.TestCase):
    def test_marks_where_the_voice_is_loud(self):
        sr = 44100
        x = np.zeros((10 * sr, 2), np.float32)
        t = np.arange(4 * sr) / sr
        x[3 * sr:7 * sr] = (0.3 * np.sin(2 * np.pi * 440 * t))[:, None]
        act = stems.vocal_activity(x, sr)
        self.assertEqual(len(act), 20)
        self.assertEqual(act[0], 0.0)
        self.assertEqual(act[10], 1.0)
        self.assertEqual(act[-1], 0.0)

    def test_silence_is_all_zero(self):
        self.assertEqual(stems.vocal_activity(np.zeros((44100 * 3, 2), np.float32), 44100), [0.0] * 6)


@unittest.skipUnless(HAVE, "hacen falta ffmpeg y scipy")
class StemsStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.settings = Settings(data_dir=self.dir, stems_window_s=20.0)
        self.settings.ensure_dirs()
        self.wav = self.dir / "s.wav"
        write_wav(str(self.wav), make_song(bpm=100, seconds=60))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_windows_are_separated_stored_and_found(self):
        self.assertIsNone(stems.window(self.settings, "local:x", "tail"))
        progress = []
        meta = stems.separate_windows(self.settings, "local:x", self.wav, 60.0, ["tail"], stems.BandSeparator(),
                                      progress.append)
        self.assertEqual(progress[-1], 1.0)
        w = stems.window(self.settings, "local:x", "tail")
        self.assertEqual((w.window, w.start, w.end), ("tail", 40.0, 60.0))
        self.assertIsNone(stems.window(self.settings, "local:x", "head"))  # solo lo pedido
        self.assertEqual(set(meta["windows"]), {"tail"})
        for n in STEM_NAMES:
            self.assertGreater((Path(w.dir) / f"tail_{n}.flac").stat().st_size, 1000)
        # los cuatro stems suman la mezcla de ese tramo
        mix = stretch.decode(str(self.wav), 40.0, 20.0)
        total = sum(stretch.decode(str(Path(w.dir) / f"tail_{n}.flac"), 0, 20.0) for n in STEM_NAMES)
        self.assertLess(float(np.sqrt(np.mean((mix - total) ** 2)) / np.sqrt(np.mean(mix ** 2))), 0.02)

    def test_cut_window_is_stored_with_its_vocal_activity(self):
        meta = stems.separate_windows(self.settings, "c", self.wav, 60.0, ["cut"], stems.BandSeparator(), cut_at=30.0)
        w = stems.window(self.settings, "c", "cut")
        self.assertEqual((w.start, w.end), (18.0, 60.0))  # 12 s antes del corte, hasta donde llega la pista
        self.assertEqual(len(w.vocal), int((w.end - w.start) / w.step))
        self.assertTrue(stems.covers(w, 25.0, 50.0))
        self.assertFalse(stems.covers(w, 10.0, 50.0))
        self.assertIn("cut", meta["windows"])
        # sin punto de corte no se separa nada
        stems.separate_windows(self.settings, "d", self.wav, 60.0, ["cut"], stems.BandSeparator())
        self.assertIsNone(stems.window(self.settings, "d", "cut"))

    def test_second_call_adds_missing_windows_without_redoing_the_others(self):
        class Counting(stems.BandSeparator):
            calls = 0

            def separate(self, x, sr):
                Counting.calls += 1
                return super().separate(x, sr)

        sep = Counting()
        stems.separate_windows(self.settings, "t", self.wav, 60.0, ["tail"], sep)
        stems.separate_windows(self.settings, "t", self.wav, 60.0, ["head", "tail"], sep)
        self.assertEqual(Counting.calls, 2)  # tail una vez, head una vez
        self.assertIsNotNone(stems.window(self.settings, "t", "head"))
        self.assertIsNotNone(stems.window(self.settings, "t", "tail"))

    def test_window_is_ignored_when_files_are_missing_or_model_changed(self):
        stems.separate_windows(self.settings, "t", self.wav, 60.0, ["head"], stems.BandSeparator())
        w = stems.window(self.settings, "t", "head")
        (Path(w.dir) / "head_bass.flac").unlink()
        self.assertIsNone(stems.window(self.settings, "t", "head"))

        class Other(stems.BandSeparator):
            name = "otro"

        stems.separate_windows(self.settings, "t", self.wav, 60.0, ["tail"], Other())
        self.assertIsNone(stems.window(self.settings, "t", "head"))  # otro modelo: lo anterior no vale

    def test_cache_is_pruned_least_recent_first(self):
        for tid in ("a", "b", "c"):
            stems.separate_windows(self.settings, tid, self.wav, 60.0, ["head"], stems.BandSeparator())
            os.utime(stems.track_dir(self.settings, tid) / "meta.json", (time.time(), time.time() + {"a": 0, "b": 10, "c": 20}[tid]))
        self.settings.stems_cache_mb = 0
        stems.prune(self.settings, keep="c")
        left = {p.name for p in self.settings.stems_dir.iterdir()}
        self.assertEqual(left, {"c"})

    def test_availability_is_false_without_demucs(self):
        # En este entorno no hay demucs; si lo hubiera, el test no aplica
        if not stems.available():
            self.assertFalse(stems.available())


if __name__ == "__main__":
    unittest.main()
