import shutil
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from autodj import adapt, stems, stretch
from autodj import strategies as S
from autodj.config import Settings
from autodj.render import (DeckSpec, RenderError, RenderSpec, StemsMissing, StemsRef, estimate_duration, fit_grid,
                           loudness_gain, render_transition)
from tests.synth import make_song, write_wav

try:
    import scipy  # noqa: F401
    HAVE_SCIPY = True
except Exception:  # noqa: BLE001
    HAVE_SCIPY = False

READY = HAVE_SCIPY and stretch.have_rubberband()


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 2 and w.getframerate() == 44100
        raw = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
    return raw.reshape(-1, 2)


class GridTest(unittest.TestCase):
    def test_fit_grid_smooths_jitter_and_ignores_outliers(self):
        rng = np.random.default_rng(1)
        db = [1.0 + 2.4 * i + rng.normal(0, 0.008) for i in range(60)]
        db[30] += 0.2  # un compás mal detectado
        g = fit_grid(db, 10, 40)
        self.assertAlmostEqual(g.length, 2.4, delta=0.002)
        self.assertAlmostEqual(g.time(30), 1.0 + 2.4 * 30, delta=0.02)
        self.assertLess(g.rms, 0.02)

    def test_too_few_bars(self):
        with self.assertRaises(RenderError):
            fit_grid([0.0, 2.0], 0, 1)

    def test_loudness_gain_matches_the_browser(self):
        self.assertAlmostEqual(loudness_gain(-11.0), 1.0)
        self.assertAlmostEqual(loudness_gain(-17.0), 10 ** (6 / 20), places=4)
        self.assertEqual(loudness_gain(None), 1.0)
        self.assertEqual(loudness_gain(-60.0), 2.5)  # tope


@unittest.skipUnless(READY, "hace falta ffmpeg con rubberband y scipy")
class RenderTest(unittest.TestCase):
    N = 8  # compases de solapamiento
    RAMP = 4

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.bpm_a, cls.bpm_b = 100.0, 112.0
        write_wav(str(cls.dir / "A.wav"), make_song(bpm=cls.bpm_a, seconds=120))
        write_wav(str(cls.dir / "B.wav"), make_song(bpm=cls.bpm_b, seconds=120, tonic="C",
                  chords=(("C", "E", "G"), ("A", "C", "E"), ("F", "A", "C"), ("G", "B", "D"))))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def spec(self, lanes=None, pitch_b=0, name="clip.wav", a_stems=None, b_stems=None):
        tp = adapt.tempo_plan(self.bpm_a, self.bpm_b)
        bar = 240 / tp.tempo
        lanes = lanes if lanes is not None else S.BarsMix()._build(0, 0, self.N * bar, 1.0, self.N, bar, True).automation
        db = lambda bpm: [i * 240 / bpm for i in range(int(120 / (240 / bpm)))]
        return RenderSpec(
            a=DeckSpec("A", str(self.dir / "A.wav"), self.bpm_a, 120.0, db(self.bpm_a), 1.0, a_stems),
            b=DeckSpec("B", str(self.dir / "B.wav"), self.bpm_b, 120.0, db(self.bpm_b), 1.0, b_stems),
            tempo=tp.tempo, mult=tp.mult, a_ramp=self.RAMP, bars=self.N, b_ramp=self.RAMP, a_idx=16, b_idx=4,
            pitch_b=pitch_b,
            lanes=[{"deck": l.deck, "param": l.param, "points": l.points} for l in lanes], out_name=name)

    def test_clip_has_the_planned_length_and_positions(self):
        sp = self.spec()
        r = render_transition(sp, self.dir)
        tp = adapt.tempo_plan(self.bpm_a, self.bpm_b)
        want = estimate_duration(self.bpm_a, tp.tempo, tp.b_eff, self.RAMP, self.N, self.RAMP)
        self.assertAlmostEqual(r["duration"], want, delta=0.001)
        x = read_wav(self.dir / "clip.wav")
        self.assertAlmostEqual(len(x) / 44100, want, delta=0.002)
        self.assertLessEqual(float(np.abs(x).max()), 1.0)
        self.assertGreater(float(np.abs(x).max()), 0.2)
        # el clip empieza en el compás 12 de A y B sigue donde acaba el clip
        self.assertAlmostEqual(r["a_start"], 12 * 2.4, delta=0.03)
        self.assertAlmostEqual(r["b_resume"], (4 + self.N + self.RAMP) * 240 / self.bpm_b, delta=0.03)
        self.assertEqual(r["engine"], "rubberband")
        # nunca queda en silencio a mitad del clip
        w = int(0.5 * 44100)
        rms = [float(np.sqrt(np.mean(x[i:i + w] ** 2))) for i in range(0, len(x) - w, w)]
        self.assertGreater(min(rms), 0.02)

    def test_the_kicks_of_both_tracks_land_together(self):
        """Se renderiza solo A y solo B con el mismo calendario y se compara dónde caen los bombos."""
        from scipy.signal import butter, fftconvolve, sosfiltfilt

        def solo(deck):
            other = "B" if deck == "A" else "A"
            lanes = [{"deck": other, "param": "gain", "points": [[0, 0.0]]},
                     {"deck": deck, "param": "gain", "points": [[0, 1.0]]}]
            sp = self.spec(name=f"solo{deck}.wav")
            sp.lanes = lanes
            r = render_transition(sp, self.dir)
            return read_wav(self.dir / f"solo{deck}.wav"), r

        xa, r = solo("A")
        xb, _ = solo("B")
        t0, t1 = r["overlap_at"], r["overlap_end"]

        def low_env(x):
            lo = sosfiltfilt(butter(2, 130, "lowpass", fs=44100, output="sos"), x.mean(1))
            return np.convolve(np.abs(lo), np.ones(132) / 132, "same")[int(t0 * 44100):int(t1 * 44100)]

        ea, eb = low_env(xa), low_env(xb)
        bar = 240 / adapt.tempo_plan(self.bpm_a, self.bpm_b).tempo
        w = int(4 * bar * 44100)
        lags = []
        for i in range(0, len(ea) - w, w):
            a, b = ea[i:i + w] - ea[i:i + w].mean(), eb[i:i + w] - eb[i:i + w].mean()
            c = fftconvolve(a, b[::-1], mode="full")
            lag = (np.arange(len(c)) - (w - 1)) / 44100
            m = np.abs(lag) < 0.04
            lags.append(1000 * lag[m][np.argmax(c[m])])
        self.assertTrue(lags)
        self.assertLess(max(abs(v) for v in lags), 4.0, f"bombos desalineados (ms): {lags}")

    def test_pitch_shift_of_the_incoming_track_glides_back(self):
        r = render_transition(self.spec(pitch_b=2, name="pitch.wav"), self.dir)
        x = read_wav(self.dir / "pitch.wav")
        self.assertAlmostEqual(len(x) / 44100, r["duration"], delta=0.002)
        self.assertTrue(np.isfinite(x).all())

    def test_irregular_beat_grid_is_refused(self):
        sp = self.spec(name="bad.wav")
        rng = np.random.default_rng(2)
        sp.a.downbeats = [t + rng.normal(0, 0.12) for t in sp.a.downbeats]
        with self.assertRaises(RenderError):
            render_transition(sp, self.dir)

    def test_spec_roundtrip(self):
        sp = self.spec(a_stems=StemsRef("/x", "tail", 10.0, 90.0))
        again = RenderSpec.from_dict(sp.to_dict())
        self.assertEqual(again, sp)

    def test_with_stems(self):
        settings = Settings(data_dir=self.dir / "data")
        settings.ensure_dirs()
        sep = stems.BandSeparator()
        # A: ventana que contiene el tramo (compases 12-32 ≈ 29–77 s); B: el principio
        settings.stems_window_s = 100.0
        stems.separate_windows(settings, "A", self.dir / "A.wav", 120.0, ["head"], sep)
        stems.separate_windows(settings, "B", self.dir / "B.wav", 120.0, ["head"], sep)
        wa, wb = stems.window(settings, "A", "head"), stems.window(settings, "B", "head")
        self.assertIsNotNone(wa)
        lanes = S.StemsMix()._build(0, 0, 0, 1.0, self.N, 240 / adapt.tempo_plan(self.bpm_a, self.bpm_b).tempo, True)
        sp = self.spec(lanes=lanes.automation, name="stems.wav",
                       a_stems=StemsRef(wa.dir, wa.window, wa.start, wa.end),
                       b_stems=StemsRef(wb.dir, wb.window, wb.start, wb.end))
        r = render_transition(sp, self.dir)
        self.assertTrue(r["stems"])
        x = read_wav(self.dir / "stems.wav")
        self.assertGreater(float(np.abs(x).max()), 0.2)
        # los stems suman la mezcla: con todos a 1 el resultado se parece al renderizado sin stems
        full = [{"deck": d, "param": p, "points": [[0, 1.0]]} for d in "AB" for p in ("drums", "bass", "other", "vocals")]
        full += [{"deck": "A", "param": "gain", "points": [[0, 1.0]]}, {"deck": "B", "param": "gain", "points": [[0, 0.0]]}]
        sp.lanes, sp.out_name = full, "stems_a.wav"
        render_transition(sp, self.dir)
        sp2 = self.spec(name="mix_a.wav")
        sp2.lanes = [{"deck": "A", "param": "gain", "points": [[0, 1.0]]}, {"deck": "B", "param": "gain", "points": [[0, 0.0]]}]
        render_transition(sp2, self.dir)
        n = int(28 * 44100)
        a, b = read_wav(self.dir / "stems_a.wav")[:n], read_wav(self.dir / "mix_a.wav")[:n]
        w = int(0.25 * 44100)
        ra = np.array([np.sqrt(np.mean(a[i:i + w] ** 2)) for i in range(0, n - w, w)])
        rb = np.array([np.sqrt(np.mean(b[i:i + w] ** 2)) for i in range(0, n - w, w)])
        self.assertLess(float(np.mean(np.abs(ra - rb) / (rb + 1e-3))), 0.2)
        self.assertGreater(float(np.corrcoef(ra, rb)[0, 1]), 0.9)

    def test_missing_stems_are_reported(self):
        sp = self.spec(name="nostems.wav", a_stems=StemsRef(str(self.dir / "nada"), "tail", 100.0, 120.0))
        with self.assertRaises(StemsMissing):
            render_transition(sp, self.dir)

    def test_echo_cut_leaves_an_echo_of_a_and_starts_b_on_the_beat(self):
        sp = self.spec(name="cut.wav")
        sp.kind, sp.bars, sp.a_ramp, sp.b_ramp, sp.mult, sp.pitch_b = "echo_cut", 2, 0, 0, 1.0, 0
        sp.a_idx, sp.b_idx, sp.lanes = 20, 0, []
        r = render_transition(sp, self.dir)
        self.assertEqual(r["kind"], "echo_cut")
        self.assertLess(r["handoff"], 0.01)
        bar_b = 240 / self.bpm_b
        self.assertAlmostEqual(r["duration"], 2 * bar_b, delta=0.002)
        self.assertAlmostEqual(r["a_start"], sp.a.downbeats[20], delta=1e-3)
        self.assertAlmostEqual(r["b_resume"], sp.b.downbeats[0] + 2 * bar_b, delta=1e-3)
        x = read_wav(self.dir / "cut.wav")
        self.assertTrue(np.isfinite(x).all())
        self.assertLessEqual(float(np.abs(x).max()), 1.0)
        # B suena desde el primer instante y el eco de A se apaga: el clip no está en silencio ni al principio ni al final
        w = int(0.25 * 44100)
        self.assertGreater(float(np.sqrt(np.mean(x[:w] ** 2))), 0.01)
        self.assertGreater(float(np.sqrt(np.mean(x[-w:] ** 2))), 0.01)
        # sin B (ganancia 0) solo queda el eco, que decae
        sp.b.trim, sp.out_name = 0.0, "echo_only.wav"
        render_transition(sp, self.dir)
        e = read_wav(self.dir / "echo_only.wav")
        seg = int(0.5 * 44100)
        env = [float(np.sqrt(np.mean(e[i:i + seg] ** 2))) for i in range(0, len(e) - seg, seg)]
        self.assertGreater(max(env), 0.005)
        self.assertLess(env[-1], max(env))

    def test_echo_cut_outside_the_grid_is_refused(self):
        sp = self.spec(name="cutbad.wav")
        sp.kind, sp.a_idx = "echo_cut", 10_000
        with self.assertRaises(RenderError):
            render_transition(sp, self.dir)

    def test_aligned_wordplay_renders_with_both_words_audible(self):
        bar = 240 / adapt.tempo_plan(self.bpm_a, self.bpm_b).tempo
        tw = 4 * bar
        lanes = S.WordplayMix()._build_aligned(self.N, bar, tw, 0.4)
        r = render_transition(self.spec(lanes=lanes, name="word.wav"), self.dir)
        x = read_wav(self.dir / "word.wav")
        self.assertAlmostEqual(len(x) / 44100, r["duration"], delta=0.002)
        self.assertTrue(np.isfinite(x).all())
        w = int(0.4 * 44100)
        rms = [float(np.sqrt(np.mean(x[i:i + w] ** 2))) for i in range(0, len(x) - w, w)]
        self.assertGreater(min(rms), 0.02)  # sin huecos
        # transición progresiva: la energía justo antes de la palabra no cae respecto a la de después
        t0 = int((r["overlap_at"] + tw) * 44100)
        before = float(np.sqrt(np.mean(x[t0 - w:t0] ** 2)))
        after = float(np.sqrt(np.mean(x[t0:t0 + w] ** 2)))
        self.assertGreater(before, 0.5 * after)
        self.assertLess(before, 2.0 * after)

    def test_relay_renders_a_vocals_over_b_instruments(self):
        settings = Settings(data_dir=self.dir / "data2")
        settings.ensure_dirs()
        settings.stems_window_s = 100.0
        sep = stems.BandSeparator()
        stems.separate_windows(settings, "A", self.dir / "A.wav", 120.0, ["head"], sep)
        stems.separate_windows(settings, "B", self.dir / "B.wav", 120.0, ["head"], sep)
        wa, wb = stems.window(settings, "A", "head"), stems.window(settings, "B", "head")
        bar = 240 / adapt.tempo_plan(self.bpm_a, self.bpm_b).tempo
        v_end = 4 * bar
        lanes = S.RelayMix()._build(0, 0, 0, 1.0, self.N, bar, True, 1.0, v_end, v_end).automation
        sp = self.spec(lanes=lanes, name="relay.wav",
                       a_stems=StemsRef(wa.dir, wa.window, wa.start, wa.end),
                       b_stems=StemsRef(wb.dir, wb.window, wb.start, wb.end))
        r = render_transition(sp, self.dir)
        x = read_wav(self.dir / "relay.wav")
        self.assertTrue(r["stems"])
        self.assertTrue(np.isfinite(x).all())
        w = int(0.4 * 44100)
        rms = [float(np.sqrt(np.mean(x[i:i + w] ** 2))) for i in range(0, len(x) - w, w)]
        self.assertGreater(min(rms), 0.02)  # nunca queda un hueco


if __name__ == "__main__":
    unittest.main()
