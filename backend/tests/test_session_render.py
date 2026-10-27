import shutil
import tempfile
import unittest
from pathlib import Path

from autodj import render, stems, stretch
from autodj.agent import DJAgent
from autodj.config import Settings
from autodj.db import Database
from autodj.library import Library
from autodj.renderer import Renderer
from autodj.session import Session
from tests.helpers import make_track
from tests.synth import make_song, write_wav
from tests.test_session import FakeClock

try:
    import scipy  # noqa: F401
    READY = stretch.have_rubberband()
except Exception:  # noqa: BLE001
    READY = False


@unittest.skipUnless(READY, "hace falta ffmpeg con rubberband y scipy")
class SessionRenderTest(unittest.IsolatedAsyncioTestCase):
    mode = "inline"

    @classmethod
    def setUpClass(cls):
        cls.audio = Path(tempfile.mkdtemp())
        write_wav(str(cls.audio / "a.wav"), make_song(bpm=100, seconds=150))
        write_wav(str(cls.audio / "b.wav"), make_song(bpm=112, seconds=150, tonic="C",
                  chords=(("C", "E", "G"), ("A", "C", "E"), ("F", "A", "C"), ("G", "B", "D"))))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.audio, ignore_errors=True)

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.settings = Settings(data_dir=Path(self.dir), enable_lyrics=False, analysis_workers=1,
                                 worker_mode=self.mode, stems_window_s=80.0)
        self.db = Database(Path(self.dir) / "t.sqlite")
        self.lib = Library(self.settings, self.db)
        for tid, bpm, key in (("a", 100, "8A"), ("b", 112, "8A")):
            t = make_track(tid, bpm=bpm, camelot=key, duration=150.0, energy=6)
            t.source, t.stream_ref = "local", str(self.audio / f"{tid}.wav")
            self.lib._save(t)
        self.renderer = Renderer(self.lib, self.settings)
        self.sent = []

        async def send(m):
            self.sent.append(m)

        self.s = Session(self.lib, self.db, DJAgent(None), send, seed=1, clock=FakeClock(), renderer=self.renderer)

    def tearDown(self):
        self.lib.close()
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def last(self, kind):
        return next((m for m in reversed(self.sent) if m["type"] == kind), None)

    async def start_playing(self):
        self.s.queue = []
        await self.s.start("auto")
        load = self.last("load")["track"]["id"]
        await self.s.handle({"type": "track_started", "track_id": load, "rate": 1.0, "offset": 0})
        await self.s.wait_planned()
        return load

    async def test_the_mix_is_rendered_by_the_worker_instead_of_falling_back_to_a_fade(self):
        first = await self.start_playing()
        plan = self.last("plan")["plan"]
        self.assertIn(plan["strategy"], ("bars", "eq"))
        self.assertEqual(plan["rate_b"], 1.0)
        r = plan["render"]
        self.assertTrue((self.settings.render_dir / r["file"]).is_file())
        self.assertEqual(r["url"], f"/api/render/{r['file']}")
        self.assertEqual(plan["out_start"], r["maps"]["a"][0][1])  # el clip empieza donde dice el plan
        self.assertAlmostEqual(r["maps"]["b"][-1][0], plan["duration"], delta=0.01)  # los mapas cubren el clip entero
        self.assertGreaterEqual(plan["out_start"], 18.0 - 1e-6)
        self.assertGreater(plan["in_start"], 0)
        kinds = [e["title"] for e in self.s.logs]
        self.assertTrue(any("Preparando la mezcla" in t for t in kinds))
        self.assertNotEqual(plan["track"]["id"], first)

    async def test_the_song_is_cut_after_its_first_chorus(self):
        await self.start_playing()
        plan = self.last("plan")["plan"]
        cur = self.s.current
        # estribillo 60–90 s en los datos de prueba: la mezcla empieza ahí y no en el outro (≈120 s)
        self.assertTrue(70.0 <= plan["out_start"] <= 100.0, plan["out_start"])
        self.assertLess(plan["out_start"], cur.duration - 45)

    async def test_shortening_can_be_switched_off(self):
        self.assertTrue(self.s._ctx().shorten)
        self.settings.mix_shorten = False
        self.assertFalse(self.s._ctx().shorten)
        self.assertFalse(self.s._ctx(("a", "b")).shorten)

    async def test_a_failed_render_falls_back_to_the_classic_plan(self):
        def boom(settings, job, progress):
            raise RuntimeError("sin memoria")

        self.lib.worker.handlers = dict(self.lib.worker.handlers, render=boom)
        await self.start_playing()
        plan = self.last("plan")["plan"]
        self.assertIsNone(plan["render"])
        self.assertEqual(plan["strategy"], "fade")  # 12 % de diferencia: el navegador no puede hacer otra cosa
        self.assertEqual(len(self.s.render_bad), 1)
        self.assertTrue(any(e["kind"] == "error" and "mezcla clásica" in e["title"] for e in self.s.logs))

    async def test_a_clip_that_arrives_too_late_is_discarded(self):
        orig = self.renderer.render

        async def late(plan, a, b, name, deadline):
            res = await orig(plan, a, b, name, deadline)
            self.s._pos = (res["a_start"] - 0.5, self.s.clock())  # ya casi estamos en el punto de entrada
            return res

        self.renderer.render = late
        await self.start_playing()
        plan = self.last("plan")["plan"]
        self.assertIsNone(plan["render"])
        self.assertTrue(any("llegó tarde" in e["title"] for e in self.s.logs))

    async def test_stems_ready_later_remix_the_pending_transition_with_stems(self):
        self.renderer.can_stems = lambda: True
        await self.start_playing()
        first = self.last("load")["track"]["id"]
        other = "b" if first == "a" else "a"
        plan = self.last("plan")["plan"]
        self.assertNotEqual(plan["strategy"], "stems")  # aún no había stems
        sep = stems.BandSeparator()
        stems.separate_windows(self.settings, first, self.audio / f"{first}.wav", 150.0, ["tail"], sep)
        stems.separate_windows(self.settings, other, self.audio / f"{other}.wav", 150.0, ["head"], sep)
        self.s._pos = (0.0, self.s.clock())
        self.s._stems_check_at = 0.0
        self.s._maybe_stems_replan()
        await self.s.wait_planned()
        plan = self.last("plan")["plan"]
        self.assertEqual(plan["strategy"], "stems")
        self.assertTrue(plan["render"]["stems"])
        self.assertEqual(plan["track"]["id"], other)  # misma pareja
        self.s._maybe_stems_replan()  # y no vuelve a rehacerse
        self.assertTrue(self.s._plan_task.done())


@unittest.skipUnless(READY, "hace falta ffmpeg con rubberband y scipy")
class NoWorkerTest(SessionRenderTest):
    """Con el worker en otro proceso que no está en marcha, todo sigue funcionando con mezclas clásicas."""

    mode = "external"

    async def test_without_a_worker_the_old_behaviour_is_kept(self):
        self.assertFalse(self.renderer.can_render())
        await self.start_playing()
        plan = self.last("plan")["plan"]
        self.assertIsNone(plan["render"])
        self.assertEqual(plan["strategy"], "fade")

    # las pruebas heredadas necesitan el worker en línea
    test_the_mix_is_rendered_by_the_worker_instead_of_falling_back_to_a_fade = None
    test_a_failed_render_falls_back_to_the_classic_plan = None
    test_the_song_is_cut_after_its_first_chorus = None
    test_shortening_can_be_switched_off = None
    test_a_clip_that_arrives_too_late_is_discarded = None
    test_stems_ready_later_remix_the_pending_transition_with_stems = None


if __name__ == "__main__":
    unittest.main()
