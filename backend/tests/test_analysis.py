import os
import shutil
import tempfile
import time
import unittest

import numpy as np

from autodj import camelot
from autodj.analysis import analyze_file, analyze_signal
from tests.synth import make_song, write_wav


class AnalysisTest(unittest.TestCase):
    def test_bpm_and_key_96_am(self):  # 96 BPM en 8A
        y = make_song(bpm=96, seconds=90)
        t0 = time.time()
        r = analyze_signal(y)
        elapsed = time.time() - t0
        self.assertLess(abs(r.bpm - 96), 0.5, r.bpm)
        self.assertIn(r.camelot, camelot.neighbours("8A") + ["9B"], r.camelot)
        self.assertLess(elapsed, 30)

    def test_downbeats_on_kick(self):
        y = make_song(bpm=100, seconds=60)
        r = analyze_signal(y)
        bar = 2.4
        phases = [(d % bar) for d in r.downbeats[2:10]]
        # el bombo fuerte cae en t = k·bar
        self.assertTrue(all(min(p, bar - p) < 0.06 for p in phases), phases)

    def test_tempo_folds_into_range(self):
        y = make_song(bpm=150, seconds=60)
        r = analyze_signal(y)
        self.assertTrue(70 <= r.bpm < 140, r.bpm)
        self.assertLess(abs(r.bpm - 75), 1.0, r.bpm)

    def test_major_key(self):
        y = make_song(bpm=110, chords=(("C", "E", "G"), ("F", "A", "C"), ("G", "B", "D"), ("C", "E", "G")),
                      tonic="C", seconds=60)
        r = analyze_signal(y)
        self.assertIn(r.camelot, ("8B", "7B", "9B", "8A"), r.camelot)

    def test_segments_cover_track_and_energy(self):
        y = make_song(bpm=96, seconds=90)
        y[: int(20 * 22050)] *= 0.2  # intro suave
        r = analyze_signal(y)
        self.assertEqual(r.segments[0].start, 0.0)
        self.assertAlmostEqual(r.segments[-1].end, r.duration, places=2)
        self.assertEqual(r.segments[0].label, "intro")
        self.assertTrue(1 <= r.energy <= 10)

    def _nearest_downbeat(self, r, t):
        idx = min(range(len(r.downbeats)), key=lambda i: abs(r.downbeats[i] - t))
        return idx, r.downbeats[idx]

    def test_intro_and_outro_land_on_the_phrase_grid(self):
        bar = 240 / 100
        y = make_song(bpm=100, seconds=64 * bar + 0.1, intro_bars=16, outro_bars=8)
        r = analyze_signal(y)
        self.assertIsNotNone(r.intro_end)
        self.assertIsNotNone(r.outro_start)
        self.assertLess(abs(r.intro_end - 16 * bar), 0.3, r.intro_end)
        self.assertLess(abs(r.outro_start - 56 * bar), 0.3, r.outro_start)
        for t in (r.intro_end, r.outro_start):
            idx, db = self._nearest_downbeat(r, t)
            self.assertLess(abs(db - t), 0.01)  # es un downbeat...
            self.assertEqual((idx - r.phrase_offset) % 8, 0)  # ...y abre una frase de 8 compases

    def test_long_intro_and_short_outro(self):
        bar = 240 / 96
        y = make_song(bpm=96, seconds=64 * bar + 0.1, intro_bars=24, outro_bars=8)
        r = analyze_signal(y)
        self.assertLess(abs(r.intro_end - 24 * bar), 0.3, r.intro_end)
        self.assertLess(abs(r.outro_start - 56 * bar), 0.3, r.outro_start)

    def test_no_intro_or_outro_when_the_song_is_flat(self):
        r = analyze_signal(make_song(bpm=120, seconds=64 * 2.0 + 0.1))
        self.assertIsNone(r.intro_end)
        self.assertIsNone(r.outro_start)

    def test_segments_follow_the_phrase_grid(self):
        bar = 240 / 100
        y = make_song(bpm=100, seconds=64 * bar + 0.1, intro_bars=16, outro_bars=8)
        r = analyze_signal(y)
        starts = [s.start for s in r.segments[1:]]
        for t in starts:  # cada frontera de segmento es un downbeat de la rejilla de 8 compases
            idx, db = self._nearest_downbeat(r, t)
            self.assertLess(abs(db - t), 0.01)
            self.assertEqual((idx - r.phrase_offset) % 8, 0, (t, idx, r.phrase_offset))

    def test_phrase_phase_prefers_the_boundary_with_most_change(self):
        from autodj.analysis import dsp

        nov = np.full(64, 0.3)
        nov[3::8] = 2.0  # los cambios ocurren siempre en el compás 3 de cada frase
        phase, conf = dsp.phrase_phase(nov)
        self.assertEqual(phase % 8, 3)
        self.assertGreater(conf, 0.5)
        # sin estructura no se inventa una fase
        flat_phase, flat_conf = dsp.phrase_phase(np.full(64, 0.5))
        self.assertEqual(flat_phase, 0)
        self.assertLess(flat_conf, 0.25)

    def test_phrase_phase_resolves_32_bar_blocks(self):
        from autodj.analysis import dsp

        nov = np.full(128, 0.2)
        nov[5::8] = 1.0  # fase 5 de las frases de 8 compases
        nov[5 + 16::32] = 3.0  # y solo cada 32 compases cambia mucho más: bloques de 32 desde el compás 21
        phase, _ = dsp.phrase_phase(nov)
        self.assertEqual(phase, 21)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg no disponible")
    def test_file_roundtrip(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "x.wav")
            write_wav(p, make_song(bpm=96, seconds=40))
            r = analyze_file(p)
            self.assertLess(abs(r.bpm - 96), 0.5)
        finally:
            shutil.rmtree(d)

    def test_too_short(self):
        with self.assertRaises(ValueError):
            analyze_signal(np.zeros(22050 * 5, np.float32))


if __name__ == "__main__":
    unittest.main()
