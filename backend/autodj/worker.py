"""Worker: ejecuta los trabajos pesados fuera de la API (análisis, letras, renderizado, stems).

Puede correr como hilos dentro de la API (`AUTODJ_WORKER=inline`, para desarrollo y tests) o como
proceso aparte en el mismo equipo:

    python -m autodj.worker                    # todas las líneas
    python -m autodj.worker --lanes render,analysis

Líneas (cada una con sus hilos, para que lo lento no retrase lo urgente):
    render      mezclas pedidas por la sesión (prioridad máxima)
    analysis    análisis de lo que ha pedido el usuario
    background  análisis y letras del catálogo, con prioridad baja y pausas
    lyrics      transcripción de lo que ha pedido el usuario
    stems       separación con Demucs; espera si hay un render en marcha
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import audio, jobs, render, stems, stretch
from .analysis import analyze_file, lyrics
from .config import Settings, settings as default_settings
from .i18n import set_lang
from .jobs import Job, JobQueue

log = logging.getLogger(__name__)

ALL_LANES = ("render", "analysis", "background", "lyrics", "stems")
HEARTBEAT_S = 4.0
IDLE_S = 0.25


def _lower_priority() -> None:
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)  # Linux: solo este hilo
    except (AttributeError, OSError):
        pass


def capabilities(settings: Settings) -> dict[str, Any]:
    try:
        import scipy  # noqa: F401
        have_scipy = True
    except Exception:  # noqa: BLE001
        have_scipy = False
    return {
        "rubberband": stretch.have_rubberband(),
        "scipy": have_scipy,
        "stems": bool(settings.stems_enabled and stems.available()),
        "lyrics": bool(settings.enable_lyrics and lyrics.available()),
        "cpus": os.cpu_count() or 1,
    }


def handle_analyze(settings: Settings, job: Job, progress: Callable[[float], None]) -> dict[str, Any]:
    p = job.payload
    path = audio.audio_path(settings, p["tid"], p["source"], p["stream_ref"])
    r = analyze_file(str(path))
    return dataclasses.asdict(r)


def handle_lyrics(settings: Settings, job: Job, progress: Callable[[float], None]) -> dict[str, Any]:
    p = job.payload
    path = audio.audio_path(settings, p["tid"], p["source"], p["stream_ref"])
    words = lyrics.transcribe(str(path), settings.whisper_model, settings.whisper_threads)
    return {"words": [dataclasses.asdict(w) for w in words]}


def handle_render(settings: Settings, job: Job, progress: Callable[[float], None]) -> dict[str, Any]:
    spec = render.RenderSpec.from_dict(job.payload["spec"])
    return render.render_transition(spec, Path(job.payload.get("out_dir") or settings.render_dir), progress)


def handle_stems(settings: Settings, job: Job, progress: Callable[[float], None]) -> dict[str, Any]:
    p = job.payload
    path = audio.audio_path(settings, p["tid"], p["source"], p["stream_ref"])
    return stems.separate_windows(settings, p["tid"], path, float(p["duration"]), list(p["which"]),
                                  progress=progress, cut_at=p.get("cut_at"))


HANDLERS: dict[str, Callable[[Settings, Job, Callable[[float], None]], dict[str, Any]]] = {
    "analyze": handle_analyze, "lyrics": handle_lyrics, "render": handle_render, "stems": handle_stems,
}


class Worker:
    def __init__(self, settings: Settings, queue: JobQueue, lanes: tuple[str, ...] = ALL_LANES,
                 handlers: dict[str, Callable] | None = None):
        self.settings, self.queue = settings, queue
        self.lanes = tuple(lanes)
        self.handlers = handlers or HANDLERS
        self.id = jobs.new_worker_id()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._bg_done = {"analyze": 0, "lyrics": 0}
        self._lock = threading.Lock()
        self.processed = 0

    def start(self) -> "Worker":
        self.queue.heartbeat(self.id, list(self.lanes), capabilities(self.settings))
        spec = {
            "render": (("render",), 1, 0, 1000, False),
            "analysis": (("analyze",), max(1, self.settings.analysis_workers), 0, jobs.P_BACKGROUND - 1, False),
            "background": (("analyze", "lyrics"), 1, jobs.P_BACKGROUND, 1000, True),
            "lyrics": (("lyrics",), 1, 0, jobs.P_BACKGROUND - 1, True),
            "stems": (("stems",), 1, 0, 1000, True),
        }
        for lane in self.lanes:
            kinds, n, lo, hi, low = spec[lane]
            for i in range(n):
                t = threading.Thread(target=self._loop, args=(lane, kinds, lo, hi, low), daemon=True,
                                     name=f"worker-{lane}-{i}")
                t.start()
                self._threads.append(t)
        hb = threading.Thread(target=self._heartbeat, daemon=True, name="worker-heartbeat")
        hb.start()
        self._threads.append(hb)
        log.info("Worker %s en marcha · líneas: %s", self.id, ", ".join(self.lanes))
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout)
        try:
            self.queue.drop_worker(self.id)
        except Exception:  # noqa: BLE001
            pass

    def wait(self) -> None:
        while not self._stop.wait(1.0):
            pass

    def _heartbeat(self) -> None:
        caps = capabilities(self.settings)
        n = 0
        try:
            while not self._stop.wait(HEARTBEAT_S):
                try:
                    self.queue.heartbeat(self.id, list(self.lanes), caps)
                    n += 1
                    if n % 4 == 0:
                        self.queue.requeue_stale()
                        self.queue.prune()
                except Exception:  # noqa: BLE001
                    log.exception("latido del worker")
        finally:
            self.queue.close()

    def _loop(self, lane: str, kinds: tuple[str, ...], lo: int, hi: int, low: bool) -> None:
        if low:
            _lower_priority()
        try:
            self._loop_inner(lane, kinds, lo, hi)
        finally:
            self.queue.close()  # la conexión es de este hilo

    def _loop_inner(self, lane: str, kinds: tuple[str, ...], lo: int, hi: int) -> None:
        while not self._stop.is_set():
            try:
                job = self.queue.claim(kinds, self.id, lo, hi)
            except Exception:  # noqa: BLE001
                log.exception("no se pudo pedir trabajo")
                self._stop.wait(1.0)
                continue
            if job is None:
                self._stop.wait(IDLE_S)
                continue
            self._run(job, lane)
            if lane == "background":
                self._throttle(job.kind)

    def _run(self, job: Job, lane: str) -> None:
        t0 = time.time()
        set_lang(job.payload.get("lang"))
        try:
            if job.kind == "stems":  # no compite con un render por la CPU
                waited = 0.0
                while self.queue.busy("render") and waited < 60 and not self._stop.is_set():
                    self._stop.wait(1.0)
                    waited += 1.0
            handler = self.handlers[job.kind]
            result = handler(self.settings, job, lambda v: self._progress(job.id, v))
            self.queue.complete(job.id, result)
        except Exception as e:  # noqa: BLE001
            log.warning("Trabajo %s %s falló: %s", job.kind, job.key, e)
            try:
                self.queue.fail(job.id, f"{type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                log.exception("no se pudo registrar el fallo")
        else:
            log.debug("Trabajo %s %s listo en %.1f s", job.kind, job.key, time.time() - t0)
        self.processed += 1

    def _progress(self, job_id: int, v: float) -> None:
        try:
            self.queue.progress(job_id, v)
        except Exception:  # noqa: BLE001
            pass

    def _throttle(self, kind: str) -> None:
        """Catálogo en segundo plano: pasadas las primeras `analysis_burst`, pausa entre una y otra."""
        with self._lock:
            self._bg_done[kind] = self._bg_done.get(kind, 0) + 1
            slow = self._bg_done[kind] > self.settings.analysis_burst
        if slow and self.settings.analysis_cooldown_s > 0:
            self._stop.wait(self.settings.analysis_cooldown_s)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m autodj.worker", description=__doc__.split("\n")[0])
    ap.add_argument("--lanes", default=",".join(ALL_LANES), help="líneas a ejecutar, separadas por comas")
    args = ap.parse_args(argv)
    lanes = tuple(x.strip() for x in args.lanes.split(",") if x.strip())
    bad = [x for x in lanes if x not in ALL_LANES]
    if bad:
        ap.error(f"líneas desconocidas: {', '.join(bad)} (válidas: {', '.join(ALL_LANES)})")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = default_settings
    s.ensure_dirs()
    worker = Worker(s, JobQueue(s.db_path), lanes).start()
    signal.signal(signal.SIGTERM, lambda *_: worker._stop.set())
    signal.signal(signal.SIGINT, lambda *_: worker._stop.set())
    try:
        worker.wait()
    finally:
        worker.stop()


if __name__ == "__main__":
    main()
