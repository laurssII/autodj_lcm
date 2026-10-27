import random
import unittest

from autodj import adapt, camelot, scoring
from autodj import strategies as S
from autodj.models import TrackView
from tests.helpers import make_track


class TempoPlanTest(unittest.TestCase):
    def test_small_gap_leaves_the_outgoing_track_alone(self):
        tp = adapt.tempo_plan(100, 104)
        self.assertEqual(tp.tempo, 100)
        self.assertEqual(tp.dev_a, 0.0)
        self.assertAlmostEqual(tp.dev_b, 100 / 104 - 1)

    def test_big_gap_meets_in_the_middle(self):
        tp = adapt.tempo_plan(100, 112)
        self.assertAlmostEqual(tp.tempo, (100 * 112) ** 0.5)
        self.assertLessEqual(tp.worst, adapt.MAX_DEV)
        self.assertGreater(tp.dev_a, 0)  # la saliente acelera
        self.assertLess(tp.dev_b, 0)  # la entrante frena

    def test_half_and_double_time(self):
        tp = adapt.tempo_plan(140, 70)
        self.assertEqual(tp.mult, 2.0)
        self.assertEqual(adapt.tempo_plan(70, 140).mult, 0.5)
        self.assertEqual(adapt.tempo_plan(100, 100).mult, 1.0)  # sin multiplicar si ya coincide

    def test_gaps_beyond_the_limit_are_rejected(self):
        self.assertIsNone(adapt.tempo_plan(100, 135))
        self.assertIsNone(adapt.tempo_plan(None, 120))
        self.assertIsNone(adapt.tempo_plan(100, 0))
        # justo en el límite cabe
        self.assertIsNotNone(adapt.tempo_plan(100, 100 * (1 + 2 * adapt.MAX_DEV) * 0.995))

    def test_ramp_bars(self):
        self.assertEqual(adapt.ramp_bars(0.005), 0)
        self.assertEqual(adapt.ramp_bars(0.03), 8)
        self.assertEqual(adapt.ramp_bars(-0.09), 12)
        self.assertEqual(adapt.ramp_bars(0.09, urgent=True), 4)


class KeyPlanTest(unittest.TestCase):
    def test_transpose_on_the_wheel(self):
        self.assertEqual(camelot.transpose("8A", 0), "8A")
        self.assertEqual(camelot.transpose("8A", 1), "3A")  # La menor +1 = Si♭ menor = 3A
        self.assertEqual(camelot.transpose("8B", 7), "9B")
        self.assertEqual(camelot.transpose(camelot.transpose("5B", 2), -2), "5B")

    def test_compatible_keys_are_not_shifted(self):
        self.assertEqual(adapt.key_plan("8A", "9A").semitones, 0)
        self.assertEqual(adapt.key_plan("8A", "8A").semitones, 0)

    def test_a_clash_is_fixed_with_a_small_shift(self):
        kp = adapt.key_plan("8A", "3A")  # 3A está un semitono por encima de 8A... y choca
        self.assertNotEqual(kp.semitones, 0)
        self.assertLessEqual(abs(kp.semitones), adapt.MAX_SHIFT)
        self.assertGreaterEqual(kp.score, 0.85)
        self.assertLess(kp.base, 0.7)
        self.assertGreaterEqual(camelot.harmonic_score("8A", camelot.transpose("3A", kp.semitones)), 0.85)

    def test_typical_detector_confidence_is_enough_to_shift(self):
        """En música real la fiabilidad de la tonalidad ronda 0,4–0,7: con ella también debe poder desplazarse."""
        for conf in (0.7, 0.5, 0.4):
            kp = adapt.key_plan("8A", "3A", conf, conf)
            self.assertNotEqual(kp.semitones, 0, conf)
            self.assertGreaterEqual(kp.score, 0.6)  # lo bastante para que "por compases" lo acepte

    def test_unreliable_keys_are_never_shifted(self):
        self.assertEqual(adapt.key_plan("8A", "3A", 0.1, 1.0).semitones, 0)
        self.assertEqual(adapt.key_plan("8A", None).semitones, 0)


class AdaptiveScoringTest(unittest.TestCase):
    def test_a_large_tempo_gap_is_no_longer_a_dead_end(self):
        classic = scoring.tempo_score(100, 112)
        adaptive = scoring.tempo_score(100, 112, adaptive=True)
        self.assertLess(classic, 0.2)
        self.assertGreater(adaptive, 0.5)
        self.assertEqual(scoring.tempo_score(100, 140, adaptive=True), 0.0)

    def test_candidates_with_adaptive_context_have_strategies(self):
        a, b = make_track("a", 100), make_track("b", 112)
        plain = scoring.candidates(a, 1.0, [b], scoring.Targets(), {}, played=set(), recent_artists=[],
                                   banned_tracks=set(), banned_artists=set(), failed=set())
        self.assertEqual(plain[0].strategies, [])  # sin worker, 12 % de diferencia solo admite un fundido
        ctx = S.RenderCtx(render=True)
        rich = scoring.candidates(a, 1.0, [b], scoring.Targets(), {}, ctx=ctx, played=set(), recent_artists=[],
                                  banned_tracks=set(), banned_artists=set(), failed=set())
        self.assertTrue({"bars", "eq"} <= set(rich[0].strategies))
        self.assertGreater(rich[0].score, plain[0].score)


class PlannerTest(unittest.TestCase):
    ctx = S.RenderCtx(render=True)

    def plan(self, a, b, name, earliest=40.0, urgent=False, ctx=None, seed=1):
        return S.STRATEGIES[name].plan(TrackView(a), b, earliest, urgent, random.Random(seed), ctx or self.ctx)

    def test_tracks_twelve_percent_apart_get_a_real_mix_not_a_fade(self):
        a, b = make_track("a", 100), make_track("b", 112)
        p = self.plan(a, b, "bars")
        self.assertIsNotNone(p.render)
        r = p.render
        self.assertAlmostEqual(r["tempo"], (100 * 112) ** 0.5, places=2)
        self.assertGreater(r["a_ramp"], 0)
        self.assertGreater(r["b_ramp"], 0)
        self.assertEqual(p.rate_b, 1.0)  # el navegador no estira nada
        # la rampa de A empieza antes del solapamiento, en un compás de A, y respeta el margen mínimo
        self.assertGreaterEqual(p.out_start, 40.0 - 0.05)
        self.assertAlmostEqual(p.out_start, a.downbeats[r["a_idx"] - r["a_ramp"]])
        self.assertGreater(p.duration, r["bars"] * 240 / r["tempo"])  # solapamiento + rampa de vuelta

    def test_without_the_worker_the_old_behaviour_is_kept(self):
        a, b = make_track("a", 100), make_track("b", 112)
        p = S.plan_transition(TrackView(a), b, None, 40.0, False, random.Random(1))
        self.assertEqual(p.strategy, "fade")
        self.assertIsNone(p.render)

    def test_the_planned_transition_goes_through_the_adaptive_planner(self):
        a, b = make_track("a", 100), make_track("b", 112)
        p = S.plan_transition(TrackView(a), b, None, 40.0, False, random.Random(1), ctx=self.ctx)
        self.assertIn(p.strategy, ("bars", "eq"))
        self.assertIsNotNone(p.render)

    def test_a_key_clash_is_resolved_by_shifting_the_incoming_track(self):
        a, b = make_track("a", 100, "8A"), make_track("b", 100, "3A")
        p = self.plan(a, b, "bars")
        self.assertIsNotNone(p)
        self.assertNotEqual(p.render["pitch_b"], 0)
        self.assertGreater(p.render["b_ramp"], 0)  # el tono necesita su deslizamiento de vuelta
        self.assertIn("semitonos", p.notes)

    def test_low_confidence_keys_still_get_a_real_mix(self):
        a, b = make_track("a", 100, "8A"), make_track("b", 100, "3A")
        a.key_conf = b.key_conf = 0.5
        p = S.plan_transition(TrackView(a), b, None, 40.0, False, random.Random(1), ctx=self.ctx)
        self.assertIsNotNone(p.render)
        self.assertNotEqual(p.render["pitch_b"], 0)

    def test_a_key_clash_that_cannot_be_fixed_gets_a_short_eq_mix_not_a_fade(self):
        a, b = make_track("a", 100, "8A"), make_track("b", 100, "2B")  # lejos en la rueda
        self.assertEqual(adapt.key_plan("8A", "2B").semitones, 0)
        p = S.plan_transition(TrackView(a), b, None, 40.0, False, random.Random(1), ctx=self.ctx)
        self.assertEqual(p.strategy, "eq")
        self.assertEqual(p.bars, 8)

    def test_explanations_name_the_cause(self):
        from autodj import adaptive
        self.assertIn("tempo", adaptive.explain(make_track("a", 100), make_track("b", 150)))
        self.assertIn("compases", adaptive.explain(make_track("a", 100, duration=20.0), make_track("b", 100)))

    def test_compatible_keys_are_not_touched(self):
        a, b = make_track("a", 100, "8A"), make_track("b", 100, "9A")
        self.assertEqual(self.plan(a, b, "bars").render["pitch_b"], 0)

    def test_tempo_beyond_the_limit_gets_an_echo_cut_not_a_fade(self):
        a, b = make_track("a", 100), make_track("b", 135)
        p = S.plan_transition(TrackView(a), b, None, 40.0, False, random.Random(1), ctx=self.ctx)
        self.assertEqual(p.strategy, "cut")
        self.assertEqual(p.render["kind"], "echo_cut")
        self.assertIn("motivo", p.notes)
        self.assertIn("tempo demasiado distinto", p.notes)
        # el corte cae sobre un compás de A, y B entra desde su primer compás
        self.assertAlmostEqual(p.out_start, a.downbeats[p.render["a_idx"]])
        self.assertEqual(p.render["b_idx"], 0)

    def test_without_a_worker_the_fade_says_why(self):
        a, b = make_track("a", 100), make_track("b", 135)
        p = S.plan_transition(TrackView(a), b, None, 40.0, False, random.Random(1),
                              ctx=S.RenderCtx(render=False))
        self.assertEqual(p.strategy, "fade")
        self.assertIn("worker", p.notes)

    def test_quick_mix_overlaps_only_four_bars(self):
        a, b = make_track("a", 100, "8A"), make_track("b", 100, "2B")
        p = self.plan(a, b, "quick")
        self.assertEqual(p.bars, 4)
        self.assertEqual(p.render["pitch_b"], 0)
        self.assertIsNone(self.plan(a, b, "quick", ctx=S.RenderCtx(render=False)))  # sin worker no existe


    def test_double_time_pairs(self):
        a, b = make_track("a", 140), make_track("b", 72)
        p = self.plan(a, b, "bars")
        self.assertEqual(p.render["mult"], 2.0)

    def test_urgent_uses_short_ramps_and_starts_soon(self):
        a, b = make_track("a", 100), make_track("b", 112)
        p = self.plan(a, b, "bars", earliest=20.0, urgent=True)
        self.assertEqual((p.render["a_ramp"], p.render["b_ramp"], p.bars), (4, 4, 8))
        self.assertLess(p.out_start, 60.0)

    def test_automation_is_relative_to_the_overlap_and_ends_with_the_outgoing_silent(self):
        a, b = make_track("a", 100), make_track("b", 112)
        p = self.plan(a, b, "eq")
        length = p.bars * 240 / p.render["tempo"]
        for lane in p.automation:
            self.assertGreaterEqual(lane.points[0][0], 0.0)
            self.assertLessEqual(lane.points[-1][0], length + 1e-3)
        gain_a = next(l for l in p.automation if l.deck == "A" and l.param == "gain")
        self.assertEqual(gain_a.points[-1][1], 0.0)

    def test_stems_strategy_needs_ready_stems_that_cover_the_mix(self):
        a, b = make_track("a", 100, duration=300.0), make_track("b", 106, duration=300.0)
        self.assertIsNone(self.plan(a, b, "stems"))  # sin stems
        wins = {("a", "tail"): S.StemWindow("/x/a", "tail", 220.0, 300.0),
                ("b", "head"): S.StemWindow("/x/b", "head", 0.0, 80.0)}
        ctx = S.RenderCtx(render=True, stems=lambda tid, w: wins.get((tid, w)))
        p = self.plan(a, b, "stems", earliest=200.0, ctx=ctx)
        self.assertIsNotNone(p)
        self.assertIsNotNone(p.render["stems_src"])
        self.assertGreaterEqual(p.out_start, 220.0 + 0.5)  # el solapamiento cae dentro de la ventana de stems
        params = {(l.deck, l.param) for l in p.automation}
        self.assertTrue({("A", "bass"), ("B", "bass"), ("A", "vocals"), ("B", "drums")} <= params)
        # con stems listos, es la preferida si nadie pide otra
        picked = S.plan_transition(TrackView(a), b, None, 200.0, False, random.Random(3), ctx=ctx)
        self.assertEqual(picked.strategy, "stems")

    def test_stems_that_do_not_reach_the_mix_are_ignored(self):
        a, b = make_track("a", 100, duration=300.0), make_track("b", 106, duration=300.0)
        wins = {("a", "tail"): S.StemWindow("/x/a", "tail", 290.0, 300.0),
                ("b", "head"): S.StemWindow("/x/b", "head", 0.0, 80.0)}
        ctx = S.RenderCtx(render=True, stems=lambda tid, w: wins.get((tid, w)))
        self.assertIsNone(self.plan(a, b, "stems", earliest=200.0, ctx=ctx))


class ShortenTest(unittest.TestCase):
    """Las canciones no se dejan enteras: la mezcla empieza tras el primer estribillo."""

    def test_cut_point_is_the_end_of_the_first_chorus(self):
        a = make_track("a", 100, duration=240.0, drop_at=60.0)  # estribillo 60–90
        self.assertAlmostEqual(S.cut_point(TrackView(a)), 90.0)

    def test_a_chorus_too_early_or_too_late_is_skipped(self):
        early = make_track("a", 100, duration=240.0, drop_at=10.0)  # acaba en 40 s: aún no ha sonado lo bastante
        self.assertIsNone(S.cut_point(TrackView(early)))
        late = make_track("a", 100, duration=240.0, drop_at=190.0)  # no queda nada que recortar
        self.assertIsNone(S.cut_point(TrackView(late)))

    def test_without_chorus_it_falls_back_to_a_phrase_near_the_middle(self):
        a = make_track("a", 100, duration=240.0, drop_at=None)
        cp = S.cut_point(TrackView(a))
        self.assertIsNotNone(cp)
        self.assertAlmostEqual(cp, 0.55 * 240, delta=10)

    def test_the_mix_starts_at_the_cut_and_not_at_the_outro(self):
        a, b = make_track("a", 100, duration=240.0), make_track("b", 106, duration=240.0)
        short = S.STRATEGIES["bars"].plan(TrackView(a), b, 40.0, False, random.Random(1),
                                          S.RenderCtx(render=True, shorten=True))
        whole = S.STRATEGIES["bars"].plan(TrackView(a), b, 40.0, False, random.Random(1),
                                          S.RenderCtx(render=True, shorten=False))
        self.assertAlmostEqual(short.out_start, 90.0, delta=15)
        self.assertGreater(whole.out_start, short.out_start + 40)
        self.assertLessEqual(short.bars, 16)

    def test_classic_mixes_shorten_too(self):
        a, b = make_track("a", 100, duration=240.0), make_track("b", 103, duration=240.0)
        p = S.plan_transition(TrackView(a), b, "bars", 40.0, False, random.Random(1),
                              ctx=S.RenderCtx(render=False, shorten=True))
        self.assertLess(p.out_start, 130)

    def test_the_fade_fallback_shortens_too(self):
        a, b = make_track("a", 100, duration=240.0), make_track("b", 150, duration=240.0)
        p = S.plan_transition(TrackView(a), b, None, 40.0, False, random.Random(1),
                              ctx=S.RenderCtx(render=False, shorten=True))
        self.assertEqual(p.strategy, "fade")
        self.assertLess(p.out_start, 130)


class WordplayTest(unittest.TestCase):
    ctx = S.RenderCtx(render=True)

    def tracks(self, b_word_at=24.6):
        a = make_track("a", 100, duration=240.0, words=[("midnight", 108.6)])
        b = make_track("b", 100, duration=240.0, words=[("midnight", b_word_at)])
        return a, b

    def plan(self, a, b, ctx=None):
        return S.STRATEGIES["wordplay"].plan(TrackView(a), b, 40.0, False, random.Random(1), ctx or self.ctx)

    def test_the_word_link_is_an_eq_mix_with_both_words_on_the_same_beat(self):
        a, b = self.tracks()
        p = self.plan(a, b)
        self.assertIsNotNone(p.render)
        self.assertEqual(p.bars, 8)
        self.assertLess(p.render["word_error_ms"], 5)
        self.assertEqual(p.render["word"], "midnight")
        # la palabra de A suena en `swap_at` s desde el inicio del solapamiento: a_idx + swap/bar = su posición
        bar = 240 / p.render["tempo"]
        a_pos = p.render["a_idx"] + p.swap_at / bar
        self.assertAlmostEqual(a_pos * 2.4, 108.6, delta=0.05)
        # y la de B cae en ese mismo instante
        b_pos = p.render["b_idx"] + p.swap_at / bar
        self.assertAlmostEqual(b_pos * 2.4, 24.6, delta=0.05)

    def test_it_is_not_abrupt_the_incoming_track_is_already_there_beneath(self):
        a, b = self.tracks()
        p = self.plan(a, b)
        lane = lambda d, par: next(l for l in p.automation if l.deck == d and l.param == par)
        gain_b = lane("B", "gain").points
        self.assertEqual(gain_b[0][1], 0.0)
        self.assertLess(gain_b[1][0], p.swap_at)  # B ya suena a pleno antes de la palabra
        low_b = lane("B", "low").points
        self.assertLessEqual(low_b[0][1], S.KILL_DB)  # pero sin graves hasta la palabra
        self.assertGreater(lane("B", "mid").points[0][1], -20)  # y con los medios atenuados, no con la voz delante
        self.assertLess(lane("B", "mid").points[0][1], 0)
        self.assertAlmostEqual(lane("A", "gain").points[-1][1], 0.0)  # al final A se ha ido

    def test_words_that_do_not_share_a_beat_position_are_skipped(self):
        a, b = self.tracks(b_word_at=24.6 + 0.8)
        self.assertIsNone(self.plan(a, b))

    def test_without_a_worker_the_old_short_link_remains(self):
        a, b = self.tracks()
        p = S.STRATEGIES["wordplay"].plan(TrackView(a), b, 40.0, False, random.Random(1), None)
        self.assertIsNone(p.render)
        self.assertLess(p.duration, 1.0)


class RelayTest(unittest.TestCase):
    """Relevo de voz: la voz de la saliente sigue sobre la base de la entrante."""

    @staticmethod
    def vocal(start, end, sings):
        """Actividad de voz cada 0,5 s en [start, end) con los tramos `sings` [(desde, hasta), ...]."""
        n = int((end - start) / 0.5)
        return [1.0 if any(a <= start + 0.5 * i < b for a, b in sings) else 0.0 for i in range(n)]

    def ctx(self, a_sings=((90, 112),), b_sings=((40, 80),), with_vocals=True):
        v = (lambda lo, hi, s: self.vocal(lo, hi, s)) if with_vocals else (lambda *_: None)
        wins = {("a", "cut"): S.StemWindow("/x/a", "cut", 78.0, 158.0, v(78.0, 158.0, a_sings), 0.5),
                ("b", "head"): S.StemWindow("/x/b", "head", 0.0, 80.0, v(0.0, 80.0, b_sings), 0.5)}
        return S.RenderCtx(render=True, shorten=True, stems=lambda tid, w: wins.get((tid, w)))

    def tracks(self):
        return make_track("a", 100, duration=240.0), make_track("b", 104, duration=240.0)

    def plan(self, ctx, a=None, b=None):
        ta, tb = self.tracks()
        return S.STRATEGIES["relay"].plan(TrackView(a or ta), b or tb, 40.0, False, random.Random(1), ctx)

    def test_a_relay_is_planned_from_the_cut_window(self):
        p = self.plan(self.ctx())
        self.assertIsNotNone(p)
        self.assertEqual(p.strategy, "relay")
        self.assertEqual(p.render["stems_src"]["a"]["window"], "cut")
        self.assertAlmostEqual(p.out_start, 90.0, delta=15)
        lane = lambda d, par: next(l for l in p.automation if l.deck == d and l.param == par)
        bar = 240 / p.render["tempo"]
        a_voc, b_voc = lane("A", "vocals").points, lane("B", "vocals").points
        # la voz de A sigue hasta v_end, y la de B no entra antes de que A acabe
        v_end = a_voc[1][0]
        self.assertGreater(v_end, p.bars * bar / 3)
        self.assertLessEqual(v_end, b_voc[1][0] + 1e-6)
        self.assertEqual((b_voc[0][1], a_voc[0][1]), (0.0, 1.0))
        # la base de A se va pronto, la de B llega pronto, y la voz de A todavía suena cuando A ya no tiene base
        self.assertLess(lane("A", "drums").points[-1][0], v_end)
        self.assertEqual(lane("A", "drums").points[-1][1], 0.0)
        self.assertEqual(lane("B", "drums").points[-1][1], 1.0)
        self.assertLess(lane("B", "drums").points[-1][0], v_end)

    def test_it_needs_a_singer_in_the_outgoing_track_at_the_start(self):
        self.assertIsNone(self.plan(self.ctx(a_sings=((120, 150),))))  # a canta, pero no durante la mezcla
        self.assertIsNone(self.plan(self.ctx(a_sings=())))  # instrumental

    def test_it_is_refused_when_the_incoming_track_sings_over_the_outgoing_one(self):
        self.assertIsNone(self.plan(self.ctx(b_sings=((0, 80),))))

    def test_without_vocal_data_there_is_no_relay(self):
        self.assertIsNone(self.plan(self.ctx(with_vocals=False)))

    def test_clashing_keys_are_not_relayed(self):
        a, b = make_track("a", 100, "8A", duration=240.0), make_track("b", 104, "2B", duration=240.0)
        self.assertFalse(S.STRATEGIES["relay"].viable(a, b, 1.0, self.ctx()))

    def test_it_is_preferred_over_plain_mixes_when_stems_are_ready(self):
        a, b = self.tracks()
        p = S.plan_transition(TrackView(a), b, None, 40.0, False, random.Random(2), ctx=self.ctx())
        self.assertIn(p.strategy, ("relay", "stems"))

class IrregularGridTest(unittest.TestCase):
    """Un tramo con compases irregulares no se elige: se busca otro en vez de caer en el fundido."""
    ctx = S.RenderCtx(render=True)

    @staticmethod
    def wobble(t, lo, hi, amount=0.3):
        """Desordena los compases lo..hi (índices) de la pista, como un detector de pulsos que se pierde."""
        for i in range(lo, hi):
            t.downbeats[i] += amount * (1 if i % 2 else -1)
        return t

    def test_a_bad_span_in_the_outgoing_track_moves_the_mix_elsewhere(self):
        a, b = make_track("a", 100), make_track("b", 104)
        base = S.STRATEGIES["bars"].plan(TrackView(a), b, 40.0, False, random.Random(1), self.ctx)
        bad = self.wobble(make_track("a", 100), base.render["a_idx"] - 6, base.render["a_idx"] + 12)
        p = S.STRATEGIES["bars"].plan(TrackView(bad), b, 40.0, False, random.Random(1), self.ctx)
        self.assertIsNotNone(p)
        from autodj.render import grid_regular
        r = p.render
        self.assertTrue(grid_regular(bad.downbeats, r["a_idx"] - r["a_ramp"], r["a_idx"] + r["bars"]))
        self.assertNotEqual(r["a_idx"], base.render["a_idx"])

    def test_a_bad_intro_in_the_incoming_track_picks_another_entry(self):
        a = make_track("a", 100)
        b = self.wobble(make_track("b", 104), 0, 20)
        p = S.STRATEGIES["bars"].plan(TrackView(a), b, 40.0, False, random.Random(1), self.ctx)
        self.assertIsNotNone(p)
        self.assertGreaterEqual(p.render["b_idx"], 14)

    def test_a_track_that_is_irregular_everywhere_ends_in_a_cut_not_a_fade(self):
        a, b = make_track("a", 100), make_track("b", 104)
        self.wobble(b, 0, len(b.downbeats) - 1)
        p = S.plan_transition(TrackView(a), b, None, 40.0, False, random.Random(1), ctx=self.ctx)
        self.assertEqual(p.strategy, "cut")

    def test_regular_tracks_are_unaffected(self):
        from autodj.render import grid_regular
        a = make_track("a", 100)
        self.assertTrue(grid_regular(a.downbeats, 10, 40))
        self.assertFalse(grid_regular(self.wobble(make_track("x", 100), 0, 60).downbeats, 10, 40))


if __name__ == "__main__":
    unittest.main()
