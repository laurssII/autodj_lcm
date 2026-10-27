import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from autodj import jobs
from autodj.config import Settings
from autodj.jobs import JobQueue
from autodj.worker import Worker


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.q = JobQueue(Path(self.dir) / "j.sqlite")

    def tearDown(self):
        self.q.close()
        shutil.rmtree(self.dir)

    def test_same_job_is_not_queued_twice_and_gets_the_best_priority(self):
        a = self.q.submit("analyze", "t1", {}, jobs.P_BACKGROUND, owner="x")
        b = self.q.submit("analyze", "t1", {}, jobs.P_USER, owner="y")
        self.assertEqual(a, b)
        j = self.q.get(a)
        self.assertEqual((j.priority, j.owner), (jobs.P_USER, "y"))
        self.assertNotEqual(self.q.submit("lyrics", "t1", {}), a)  # otro tipo, otro trabajo

    def test_priority_order_and_lane_filters(self):
        self.q.submit("analyze", "bg", {}, jobs.P_BACKGROUND)
        self.q.submit("analyze", "user", {}, jobs.P_USER)
        self.q.submit("render", "r", {}, jobs.P_RENDER)
        self.assertEqual(self.q.claim(["analyze", "render"], "w").key, "r")
        self.assertEqual(self.q.claim(["analyze"], "w", 0, jobs.P_BACKGROUND - 1).key, "user")
        self.assertIsNone(self.q.claim(["analyze"], "w", 0, jobs.P_BACKGROUND - 1))  # el de fondo no es de esta línea
        self.assertEqual(self.q.claim(["analyze"], "w", jobs.P_BACKGROUND).key, "bg")

    def test_two_workers_never_take_the_same_job(self):
        for i in range(20):
            self.q.submit("analyze", f"t{i}", {}, jobs.P_USER)
        taken, lock = [], threading.Lock()

        def run(name):
            q = JobQueue(self.q.path)
            while (j := q.claim(["analyze"], name)):
                with lock:
                    taken.append(j.key)
            q.close()

        ts = [threading.Thread(target=run, args=(f"w{i}",)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(taken), sorted(f"t{i}" for i in range(20)))

    def test_bump_only_affects_queued_jobs(self):
        self.q.submit("analyze", "a", {}, jobs.P_BACKGROUND)
        self.assertTrue(self.q.bump("analyze", "a", jobs.P_USER))
        self.assertFalse(self.q.bump("analyze", "a", jobs.P_USER))  # ya tiene esa prioridad
        self.q.claim(["analyze"], "w")
        self.assertFalse(self.q.bump("analyze", "a", jobs.P_RENDER))  # ya empezó

    def test_cancel_only_queued(self):
        a = self.q.submit("render", "x", {})
        self.q.cancel(a)
        self.assertEqual(self.q.get(a).status, jobs.CANCELLED)
        self.assertIsNone(self.q.claim(["render"], "w"))

    def test_jobs_of_a_dead_worker_go_back_to_the_queue(self):
        self.q.heartbeat("w1", ["render"], {})
        a = self.q.submit("render", "x", {})
        self.q.claim(["render"], "w1")
        self.assertEqual(self.q.requeue_stale(60), 0)  # el worker sigue vivo
        self.q._tx(lambda c: c.execute("UPDATE workers SET beat=?", (time.time() - 120,)))
        self.assertEqual(self.q.requeue_stale(60), 1)
        self.assertEqual(self.q.get(a).status, jobs.QUEUED)

    def test_capabilities_come_from_live_workers_only(self):
        self.assertEqual(self.q.capabilities(), {})
        self.q.heartbeat("w1", ["render"], {"rubberband": True, "scipy": False, "cpus": 4})
        self.q.heartbeat("w2", ["stems"], {"rubberband": False, "scipy": True, "stems": False})
        caps = self.q.capabilities()
        self.assertTrue(caps["rubberband"] and caps["scipy"])
        self.assertFalse(caps["stems"])
        self.q._tx(lambda c: c.execute("UPDATE workers SET beat=?", (time.time() - 100,)))
        self.assertEqual(self.q.capabilities(), {})

    def test_results_are_applied_once_and_by_their_owner(self):
        a = self.q.submit("analyze", "t", {}, owner="api1")
        self.q.claim(["analyze"], "w")
        self.assertEqual([j.id for j in self.q.unseen(["analyze"], "api1")], [a])  # en marcha, aún sin ver
        self.q.mark_seen(a, 1)
        self.assertEqual(self.q.unseen(["analyze"], "api1"), [])
        self.q.complete(a, {"ok": 1})
        self.assertEqual(self.q.unseen(["analyze"], "api2"), [])  # de otra API
        self.assertEqual(self.q.adopt_unseen(["analyze"], "api2"), 1)  # arranque posterior: se hace cargo
        self.assertEqual(self.q.unseen(["analyze"], "api2")[0].result, {"ok": 1})
        self.q.mark_seen(a, 2)
        self.assertEqual(self.q.unseen(["analyze"], "api2"), [])

    def test_prune_keeps_what_has_not_been_applied(self):
        a = self.q.submit("analyze", "t", {})
        b = self.q.submit("render", "r", {})
        for j in (a, b):
            self.q.claim(["analyze", "render"], "w")
        self.q.complete(a, {})
        self.q.complete(b, {})
        self.q.prune(keep_seconds=-1)
        self.assertIsNotNone(self.q.get(a))  # análisis sin aplicar: se conserva
        self.assertIsNone(self.q.get(b))  # un render terminado no hace falta


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.settings = Settings(data_dir=Path(self.dir), analysis_workers=1)
        self.settings.ensure_dirs()
        self.q = JobQueue(Path(self.dir) / "j.sqlite")
        self.w = None

    def tearDown(self):
        if self.w:
            self.w.stop()
        self.q.close()
        shutil.rmtree(self.dir)

    def test_runs_jobs_reports_results_and_failures(self):
        def ok(settings, job, progress):
            progress(0.5)
            return {"echo": job.payload["x"]}

        def boom(settings, job, progress):
            raise RuntimeError("fallo de prueba")

        self.w = Worker(self.settings, self.q, ("render", "analysis"), handlers={"render": ok, "analyze": boom}).start()
        a = self.q.submit("render", "r1", {"x": 7})
        b = self.q.submit("analyze", "t1", {})
        ja, jb = self.q.wait(a, 10), self.q.wait(b, 10)
        self.assertEqual((ja.status, ja.result), (jobs.DONE, {"echo": 7}))
        self.assertEqual(jb.status, jobs.FAILED)
        self.assertIn("fallo de prueba", jb.error)
        self.assertTrue(self.q.workers())  # latido registrado
        self.assertIn("cpus", self.q.capabilities())

    def test_a_render_does_not_wait_behind_a_slow_analysis(self):
        release = threading.Event()

        def slow(settings, job, progress):
            release.wait(10)
            return {}

        self.w = Worker(self.settings, self.q, ("render", "analysis"),
                        handlers={"analyze": slow, "render": lambda s, j, p: {"ok": True}}).start()
        slow_id = self.q.submit("analyze", "big", {}, jobs.P_USER)
        time.sleep(0.5)
        r = self.q.wait(self.q.submit("render", "r", {}, jobs.P_RENDER), 5)
        self.assertEqual(r.status, jobs.DONE)  # el render salió mientras el análisis seguía ocupando su hilo
        self.assertEqual(self.q.get(slow_id).status, jobs.RUNNING)
        release.set()

    def test_stems_wait_for_a_render_in_progress(self):
        order = []
        gate = threading.Event()

        def render(settings, job, progress):
            gate.wait(5)
            order.append("render")
            return {}

        def stem(settings, job, progress):
            order.append("stems")
            return {}

        self.w = Worker(self.settings, self.q, ("render", "stems"), handlers={"render": render, "stems": stem}).start()
        self.q.submit("render", "r", {}, jobs.P_RENDER)
        time.sleep(0.5)
        s = self.q.submit("stems", "t", {}, jobs.P_NEAR)
        time.sleep(1.5)
        self.assertEqual(self.q.get(s).status, jobs.RUNNING)  # reclamado pero esperando
        self.assertEqual(order, [])
        gate.set()
        self.q.wait(s, 10)
        self.assertEqual(order, ["render", "stems"])


if __name__ == "__main__":
    unittest.main()
