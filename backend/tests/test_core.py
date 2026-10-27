import random
import unittest

from autodj import camelot
from autodj.models import TrackView
from autodj.scoring import Targets, candidates, weighted_pick
from autodj.strategies import STRATEGIES, plan_transition, viable_strategies
from tests.helpers import make_track


class CamelotTest(unittest.TestCase):
    def test_key_mapping(self):
        self.assertEqual(camelot.key_to_camelot("A", "minor"), "8A")
        self.assertEqual(camelot.key_to_camelot("C", "major"), "8B")
        self.assertEqual(camelot.key_to_camelot("F#", "minor"), "11A")
        self.assertEqual(camelot.key_to_camelot("Db", "major"), "3B")
        self.assertEqual(camelot.camelot_to_key("8A"), "Am")

    def test_neighbours_wrap(self):  # 12A → 11A, 1A, 12B
        self.assertEqual(set(camelot.neighbours("12A")), {"12A", "11A", "1A", "12B"})

    def test_relations(self):
        self.assertEqual(camelot.relation("8A", "9A"), "adjacent")
        self.assertEqual(camelot.relation("1A", "12A"), "adjacent")
        self.assertEqual(camelot.relation("8A", "8B"), "relative")
        self.assertEqual(camelot.relation("8A", "10A"), "boost2")
        self.assertEqual(camelot.relation("8A", "3A"), "boost7")
        self.assertEqual(camelot.relation("8A", "9B"), "diagonal")
        self.assertEqual(camelot.relation("8A", "5B"), "clash")

    def test_low_confidence_softens(self):
        self.assertAlmostEqual(camelot.harmonic_score("8A", "8A", 0.0, 1.0), 0.5)
        self.assertEqual(camelot.harmonic_score(None, "8A"), 0.5)


class StrategyTest(unittest.TestCase):
    def setUp(self):
        self.a = make_track("a", bpm=100, camelot="8A")

    def _check_plan(self, plan, a_view, b, earliest):
        self.assertGreaterEqual(plan.out_start, earliest - 1e-6)
        self.assertLessEqual(plan.out_start + plan.duration, a_view.duration)
        # la transición empieza en un downbeat de A
        self.assertTrue(any(abs(plan.out_start - d) < 1e-3 for d in a_view.downbeats))

    def test_bars_aligns_downbeats(self):
        b = make_track("b", bpm=103, camelot="9A")
        av = TrackView(self.a)
        plan = STRATEGIES["bars"].plan(av, b, earliest=30, rng=random.Random(1))
        self.assertIsNotNone(plan)
        self._check_plan(plan, av, b, 30)
        self.assertAlmostEqual(plan.rate_b, 100 / 103, places=3)
        bv = TrackView(b, plan.rate_b)
        self.assertTrue(any(abs(plan.in_start - d) < 1e-3 for d in bv.downbeats))
        # B sonará al tempo de A: los beats coinciden dentro de 10 ms durante toda la mezcla
        self.assertAlmostEqual(bv.bpm, av.bpm, places=1)
        # Nunca dos líneas de graves a la vez más de una rampa corta
        lows = {l.deck: l.points for l in plan.automation if l.param == "low"}
        swap_b = next(t for t, v in lows["B"] if v == 0.0)
        kill_a = next(t for t, v in lows["A"] if v < -30)
        self.assertLess(kill_a - swap_b, 4 * 60 / 100)

    def test_bars_not_viable_for_big_tempo_gap(self):
        b = make_track("b", bpm=124, camelot="8A")
        self.assertNotIn("bars", viable_strategies(self.a, b))
        self.assertNotIn("eq", viable_strategies(self.a, b))
        self.assertNotIn("drop", STRATEGIES)

    def test_big_tempo_gap_falls_back_to_soft_fade(self):
        b = make_track("b", bpm=124, drop_at=60.0)
        plan = plan_transition(TrackView(self.a), b, None, earliest=30)
        self.assertEqual(plan.strategy, "fade")
        self.assertGreaterEqual(plan.duration, 4.0)  # nunca un corte en seco
        gain_a = next(l for l in plan.automation if l.deck == "A" and l.param == "gain").points
        self.assertEqual(gain_a[-1][1], 0.0)
        self.assertGreater(gain_a[-1][0], 4.0)

    def _idx(self, view, t):
        db = view.downbeats
        i = min(range(len(db)), key=lambda k: abs(db[k] - t))
        self.assertLess(abs(db[i] - t), 1e-3, "no cae en un downbeat")
        return i

    def test_transition_starts_on_the_largest_available_phrase(self):
        a = make_track("a", bpm=100, duration=240.0, phrase_offset=5, outro_start=64 * 2.4 + 5 * 2.4)
        av = TrackView(a)
        plan = STRATEGIES["eq"].plan(av, make_track("b", bpm=100), earliest=30, rng=random.Random(2))
        i = self._idx(av, plan.out_start)
        self.assertEqual((i - 5) % 8, 0, f"compás {i} no abre una frase de 8 compases (fase 5)")

    def test_swap_lands_on_the_body_of_b_and_the_outro_of_a(self):
        bar = 2.4  # 100 BPM
        a = make_track("a", bpm=100, duration=200.0, outro_start=64 * bar)
        b = make_track("b", bpm=100, camelot="8A", duration=200.0, intro_end=12 * bar)
        av = TrackView(a)
        plan = STRATEGIES["eq"].plan(av, b, earliest=30, rng=random.Random(1))
        self.assertTrue(plan.aligned)
        self.assertEqual(plan.bars, 16)  # la intro de 12 compases admite un cambio a los 8, no a los 16
        # el cambio de graves cae en el outro de A...
        self.assertAlmostEqual(plan.out_start + plan.swap_at, 64 * bar, delta=1e-3)
        # ...y B está justo donde acaba su intro
        self.assertAlmostEqual(plan.in_start + plan.swap_at, 12 * bar, delta=1e-3)

    def test_long_intro_allows_32_bars_only_with_an_intro_to_lean_on(self):
        bar = 2.4
        a = make_track("a", bpm=100, duration=240.0, outro_start=96 * bar)
        long_intro = make_track("b", bpm=100, duration=240.0, intro_end=16 * bar)
        plan = STRATEGIES["eq"].plan(TrackView(a), long_intro, earliest=30, rng=random.Random(0))
        self.assertEqual((plan.bars, plan.aligned), (32, True))
        no_intro = make_track("c", bpm=100, duration=240.0)
        plan = STRATEGIES["eq"].plan(TrackView(a), no_intro, earliest=30, rng=random.Random(0))
        self.assertEqual((plan.bars, plan.aligned), (16, False))  # sin intro no se pasa de 16

    def test_unaligned_entry_uses_a_phrase_of_b(self):
        b = make_track("b", bpm=100, phrase_offset=3)
        plan = STRATEGIES["bars"].plan(TrackView(self.a), b, earliest=30, rng=random.Random(1))
        self.assertFalse(plan.aligned)
        bv = TrackView(b, plan.rate_b)
        i = self._idx(bv, plan.in_start)
        self.assertEqual((i - 3) % 8, 0)

    def test_bass_swap_is_one_downbeat_and_never_both_basses(self):
        b = make_track("b", bpm=100, camelot="8A", intro_end=16 * 2.4)
        for name in ("bars", "eq"):
            plan = STRATEGIES[name].plan(TrackView(self.a), b, earliest=30, rng=random.Random(3))
            lows = {l.deck: l.points for l in plan.automation if l.param == "low"}
            swap = plan.swap_at
            # B no tiene graves hasta el cambio y A los pierde en ese mismo instante
            self.assertEqual(lows["B"][0][1], -40.0)
            self.assertEqual(lows["B"][1][0], swap)
            self.assertEqual(lows["A"][1][0], swap)
            # el cambio cae en un compás entero de la mezcla
            self.assertAlmostEqual(swap / (4 * 60 / 100) % 1, 0.0, places=3)

    def test_gain_curves_keep_constant_power(self):
        b = make_track("b", bpm=100, camelot="8A")
        plan = STRATEGIES["bars"].plan(TrackView(self.a), b, earliest=30, rng=random.Random(1))
        ga = next(l.points for l in plan.automation if l.deck == "A" and l.param == "gain")
        gb = next(l.points for l in plan.automation if l.deck == "B" and l.param == "gain")
        for (ta, va), (tb, vb) in zip(ga, gb):
            self.assertAlmostEqual(ta, tb)
            self.assertAlmostEqual(va ** 2 + vb ** 2, 1.0, places=2)  # sin hundimiento a mitad

    def test_wordplay(self):
        a = make_track("a", words=[("corazón", 150.0), ("noche", 40.0)])
        b = make_track("b", bpm=90, words=[("corazon", 20.0)])
        self.assertIn("wordplay", viable_strategies(a, b))
        plan = STRATEGIES["wordplay"].plan(TrackView(a), b, earliest=30)
        self.assertEqual((plan.out_start, plan.in_start), (150.0, 20.0))

    def test_fade_fallback_starts_on_a_phrase_near_a_outro(self):
        bar = 2.4
        a = make_track("a", bpm=100, duration=240.0, outro_start=80 * bar, phrase_offset=5)
        b = make_track("b", bpm=150, camelot="2B")
        av = TrackView(a)
        plan = plan_transition(av, b, None, earliest=30)
        self.assertEqual(plan.strategy, "fade")
        i = self._idx(av, plan.out_start)
        self.assertEqual((i - 5) % 4, 0)  # frontera de frase, no un compás cualquiera
        self.assertLess(abs(plan.out_start - 80 * bar), 8 * bar)  # y cerca de donde A acaba

    def test_wordplay(self):
        a = make_track("a", words=[("corazón", 150.0), ("noche", 40.0)])
        b = make_track("b", bpm=90, words=[("corazon", 20.0)])
        self.assertIn("wordplay", viable_strategies(a, b))
        plan = STRATEGIES["wordplay"].plan(TrackView(a), b, earliest=30)
        self.assertEqual((plan.out_start, plan.in_start), (150.0, 20.0))

    def test_plan_always_returns(self):
        b = make_track("b", bpm=150, camelot="2B", drop_at=None)
        b.segments = []
        plan = plan_transition(TrackView(self.a), b, None, earliest=190)
        self.assertEqual(plan.strategy, "fade")

    def test_urgent_mixes_soon(self):
        b = make_track("b", bpm=100, camelot="8A")
        plan = plan_transition(TrackView(self.a), b, "bars", earliest=40, urgent=True)
        self.assertLess(plan.out_start - 40, 4 * 60 / 100 * 8 + 0.01)


class ScoringTest(unittest.TestCase):
    def test_hard_rules_and_order(self):
        cur = make_track("cur", bpm=100, camelot="8A", artist="X")
        lib = [
            cur,
            make_track("good", bpm=101, camelot="9A"),
            make_track("clash", bpm=100, camelot="2B"),
            make_track("same_artist", bpm=100, camelot="8A", artist="X"),
            make_track("played", bpm=100, camelot="8A"),
        ]
        cands = candidates(cur, 1.0, lib, Targets(), {}, played={"played"}, recent_artists=["X"],
                           banned_tracks=set(), banned_artists=set(), failed=set())
        ids = [c.track.id for c in cands]
        self.assertEqual(ids[0], "good")
        self.assertNotIn("same_artist", ids)
        self.assertNotIn("played", ids)

    def test_novelty_penalty(self):
        cur = make_track("cur")
        lib = [make_track("b"), make_track("c")]
        cands = candidates(cur, 1.0, lib, Targets(), {("cur", "b"): 5}, played=set(), recent_artists=[],
                           banned_tracks=set(), banned_artists=set(), failed=set())
        self.assertEqual(cands[0].track.id, "c")

    def test_weighted_pick_varies(self):
        lib = [make_track(str(i), energy=6) for i in range(10)]
        cands = candidates(None, 1.0, lib, Targets(), {}, played=set(), recent_artists=[],
                           banned_tracks=set(), banned_artists=set(), failed=set())
        rng = random.Random(0)
        picks = {weighted_pick(cands, 0.1, rng).track.id for _ in range(50)}
        self.assertGreater(len(picks), 5)


if __name__ == "__main__":
    unittest.main()
