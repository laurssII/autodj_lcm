import os
import shutil
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

from autodj.config import Settings
from autodj.db import Database
from autodj import jobs as jobs_mod
from autodj import library as library_mod
from autodj.library import Library
from autodj.models import Track
from autodj.worker import Worker
from tests.synth import make_song, write_wav


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg no disponible")
class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.settings = Settings(data_dir=Path(self.dir), enable_lyrics=False, analysis_workers=1)
        self.db = Database(Path(self.dir) / "t.sqlite")
        self.events = []
        self.lib = Library(self.settings, self.db, notify=lambda t: self.events.append(t.status))
        # (orden inverso: primero se cierran las bibliotecas que cada prueba abra después, luego esta, la base y la carpeta)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(self.db.close)
        self.addCleanup(self.lib.close)

    def _wav_bytes(self, bpm=96):
        p = os.path.join(self.dir, "in.wav")
        write_wav(p, make_song(bpm=bpm, seconds=45))
        return Path(p).read_bytes()

    def _wait(self, tid, timeout=30):
        t0 = time.time()
        while time.time() - t0 < timeout:
            t = self.lib.get(tid)
            if t.status in ("ready", "needs_review", "error"):
                return t
            time.sleep(0.1)
        self.fail("el análisis no terminó")

    def test_upload_analyze_stretch_and_dedupe(self):
        data = self._wav_bytes()
        t = self.lib.add_upload("Artista X - Mi tema.wav", data)
        self.assertEqual((t.title, t.artist), ("Mi tema", "Artista X"))
        done = self._wait(t.id)
        self.assertIn(done.status, ("ready", "needs_review"), done.status_detail)
        self.assertLess(abs(done.bpm - 96), 0.5)
        self.assertEqual(self.events[:2], ["pending", "analyzing"])
        # duplicado: misma pista, sin reanalizar
        again = self.lib.add_upload("otro nombre.wav", data)
        self.assertEqual(again.id, t.id)
        # time-stretch con ffmpeg
        p = self.lib.stretched_path(done, 1.03)
        self.assertTrue(p.exists() and p.stat().st_size > 1000)
        # persistencia
        lib2 = Library(self.settings, self.db)
        self.addCleanup(lib2.close)
        self.assertEqual(lib2.get(t.id).bpm, done.bpm)

    def _structured_wav(self):
        p = os.path.join(self.dir, "structured.wav")
        write_wav(p, make_song(bpm=100, seconds=64 * 2.4 + 0.1, intro_bars=16, outro_bars=8))
        return Path(p).read_bytes()

    def test_analysis_keeps_phrase_grid_intro_and_outro(self):
        t = self._wait(self.lib.add_upload("tema.wav", self._structured_wav()).id)
        self.assertAlmostEqual(t.intro_end, 16 * 2.4, delta=0.3)
        self.assertAlmostEqual(t.outro_start, 56 * 2.4, delta=0.3)
        self.assertTrue(t.analysis_version.startswith("1.1-"))
        # persisten en la base de datos
        again = Library(self.settings, self.db).get(t.id)
        self.assertEqual((again.intro_end, again.outro_start, again.phrase_offset),
                         (t.intro_end, t.outro_start, t.phrase_offset))

    def test_outdated_analysis_is_refreshed_without_taking_the_track_offline(self):
        t = self._wait(self.lib.add_upload("tema.wav", self._structured_wav()).id)
        # Una pista analizada con la versión 1.0: sin rejilla de frases ni intro/outro.
        t.analysis_version, t.intro_end, t.outro_start, t.phrase_offset = "1.0-numpy", None, None, 0
        self.lib._save(t)
        seen = []
        lib2 = Library(self.settings, self.db, notify=lambda x: seen.append(x.status))
        self.addCleanup(lib2.close)
        deadline = time.time() + 30
        while time.time() < deadline and not (lib2.get(t.id).analysis_version or "").startswith("1.1-"):
            time.sleep(0.1)
        fresh = lib2.get(t.id)
        self.assertTrue(fresh.analysis_version.startswith("1.1-"), fresh.analysis_version)
        self.assertIsNotNone(fresh.intro_end)
        self.assertTrue(fresh.playable)
        # mientras tanto nunca salió de "ready": una pista reproducible no se retira para reanalizarla
        self.assertNotIn("analyzing", seen)
        self.assertNotIn("pending", seen)

    def test_up_to_date_tracks_are_not_reanalyzed(self):
        t = self._wait(self.lib.add_upload("tema.wav", self._wav_bytes()).id)
        seen = []
        lib2 = Library(self.settings, self.db, notify=lambda x: seen.append(x.status))
        self.addCleanup(lib2.close)
        time.sleep(0.5)
        self.assertEqual(seen, [])
        self.assertEqual(lib2.get(t.id).analysis_version, t.analysis_version)

    def test_stretch_cache_is_capped(self):
        t = self.lib.add_upload("a.wav", self._wav_bytes())
        t = self._wait(t.id)
        self.settings.stretch_cache_mb = 0  # cualquier archivo supera el tope
        first = self.lib.stretched_path(t, 1.02)
        second = self.lib.stretched_path(t, 1.04)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())

    def test_rejects_unknown_format(self):
        with self.assertRaises(ValueError):
            self.lib.add_upload("notas.txt", b"hola")

    def test_overrides(self):
        t = self.lib.add_upload("a.wav", self._wav_bytes())
        self._wait(t.id)
        t = self.lib.update_overrides(t.id, {"camelot": "5b", "bpm": 98})
        self.assertEqual((t.eff_camelot, t.eff_bpm), ("5B", 98.0))
        with self.assertRaises(ValueError):
            self.lib.update_overrides(t.id, {"camelot": "13A"})

    def test_retry_requeues_a_failed_track(self):
        t = self.lib.add_upload("roto.wav", b"esto no es audio")
        self.assertEqual(self._wait(t.id).status, "error")
        self.events.clear()
        self.assertIsNone(self.lib.retry("no-existe"))
        self.lib.retry(t.id)
        self.assertEqual(self.events[0], "pending")  # vuelve a la cola de análisis
        self.assertEqual(self._wait(t.id).status, "error")  # y, al seguir roto, vuelve a fallar
        ok = self.lib.add_upload("bien.wav", self._wav_bytes())
        self.assertEqual(self._wait(ok.id).status in ("ready", "needs_review"), True)
        self.lib.retry(ok.id)  # una pista sana no se toca
        self.assertIn(self.lib.get(ok.id).status, ("ready", "needs_review"))

    def _block_background(self):
        """Ocupa la línea de segundo plano del worker hasta soltar el evento (simula un análisis largo del catálogo)."""
        release = threading.Event()
        started = threading.Event()
        w = self.lib.worker
        orig = w.handlers["analyze"]

        def handler(settings, job, progress):
            if job.key == "bloqueo":
                started.set()
                release.wait(20)
                return {}
            return orig(settings, job, progress)

        w.handlers = dict(w.handlers, analyze=handler)
        self.lib.jobs.submit("analyze", "bloqueo", {"tid": "bloqueo", "source": "local", "stream_ref": "x"},
                             jobs_mod.P_BACKGROUND)
        self.assertTrue(started.wait(5))
        return release

    def test_lo_que_pide_el_usuario_no_espera_al_catalogo(self):
        release = self._block_background()
        try:
            t = self.lib.add_upload("mia.wav", self._wav_bytes())  # primer plano
            self.assertIn(self._wait(t.id).status, ("ready", "needs_review"))
        finally:
            release.set()

    def test_prioritize_saca_una_pista_del_catalogo_de_la_cola_de_fondo(self):
        path = os.path.join(self.dir, "cat.wav")
        write_wav(path, make_song(bpm=100, seconds=45))
        t = Track(id="local:cat", source="local", stream_ref=path, title="Catálogo", artist="x")
        self.lib._save(t)
        release = self._block_background()
        try:
            self.lib._enqueue(t.id, background=True)  # detrás del trabajo bloqueado
            time.sleep(0.5)
            self.assertEqual(self.lib.get(t.id).status, "pending")
            self.lib.prioritize(t.id)  # la ha pedido el usuario
            self.assertIn(self._wait(t.id).status, ("ready", "needs_review"))
        finally:
            release.set()
        time.sleep(0.5)  # el trabajo de fondo ya no la repite
        self.assertIn(self.lib.get(t.id).status, ("ready", "needs_review"))

    def test_pausa_entre_pistas_de_fondo_pasada_la_rafaga(self):
        self.settings.analysis_burst, self.settings.analysis_cooldown_s = 2, 7.0
        waits = []
        w = Worker(self.settings, self.lib.jobs, lanes=())  # sin hilos: solo se prueba la pausa
        w._stop = types.SimpleNamespace(wait=waits.append)
        for _ in range(5):
            w._throttle("analyze")
        self.assertEqual(waits, [7.0, 7.0, 7.0])  # las 2 primeras sin pausa
        self.assertEqual(w._bg_done["lyrics"], 0)  # cada tipo de trabajo cuenta aparte

    def test_el_catalogo_inicial_es_pequeno_y_mezcla_fuentes(self):
        from autodj import sources

        mk = lambda src, i: sources.RemoteTrack(f"{src}:{i}", src, str(i), f"t{i}", "a", None, 100, None, None)
        orig = (sources.audius_tracks, sources.jamendo_tracks)
        sources.audius_tracks = lambda s, limit: [mk("audius", i) for i in range(limit)]
        sources.jamendo_tracks = lambda s, limit: [mk("jamendo", i) for i in range(limit)]
        enqueued = []
        self.lib._enqueue = lambda tid, refresh=False, background=False: enqueued.append((tid, background))
        try:
            self.assertEqual(self.lib.import_catalog(6), 6)
        finally:
            sources.audius_tracks, sources.jamendo_tracks = orig
        self.assertEqual(sorted({t.split(":")[0] for t, _ in enqueued}), ["audius", "jamendo"])
        self.assertTrue(all(bg for _, bg in enqueued))  # el catálogo siempre va en segundo plano


if __name__ == "__main__":
    unittest.main()
