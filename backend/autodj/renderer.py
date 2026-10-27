"""Puente entre la sesión y el worker para las mezclas adaptativas.

La sesión decide *qué* mezcla hacer (`adaptive.py`); este módulo convierte esa decisión en un trabajo
de renderizado, espera el clip y gestiona los stems. No hace DSP: eso ocurre en el worker.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

from . import audio, stems
from . import jobs as jobs_mod
from .config import Settings
from .i18n import get_lang, tr
from .library import Library
from .models import Track
from .render import DeckSpec, RenderError, RenderSpec, StemsRef, loudness_gain
from .strategies import MixPlan, RenderCtx, StemWindow

log = logging.getLogger(__name__)

CLIP_RE = re.compile(r"^[A-Za-z0-9_-]{4,40}\.wav$")
CAPS_TTL = 2.0


class Renderer:
    def __init__(self, library: Library, settings: Settings):
        self.library, self.settings = library, settings
        self.jobs = library.jobs
        self._caps: tuple[float, dict[str, Any]] = (0.0, {})

    def caps(self) -> dict[str, Any]:
        at, caps = self._caps
        if time.time() - at > CAPS_TTL:
            try:
                caps = self.jobs.capabilities()
            except Exception:  # noqa: BLE001
                log.exception("no se pudieron leer las capacidades del worker")
                caps = {}
            self._caps = (time.time(), caps)
        return caps

    def can_render(self) -> bool:
        c = self.caps()
        return bool(self.settings.mix_adaptive and c.get("rubberband") and c.get("scipy"))

    def can_stems(self) -> bool:
        return bool(self.settings.stems_enabled and self.can_render() and self.caps().get("stems"))

    def ctx(self) -> RenderCtx:
        return RenderCtx(render=self.can_render(), stems=self.stems_window if self.can_stems() else None)

    def stems_window(self, tid: str, which: str) -> StemWindow | None:
        return stems.window(self.settings, tid, which)

    def request_stems(self, t: Track, which: tuple[str, ...] = ("head", "tail"), priority: int = jobs_mod.P_NEAR,
                      cut_at: float | None = None) -> bool:
        """Pide separar los tramos que falten. Devuelve True si hay algo pendiente (en marcha o recién pedido).

        `cut` es el tramo alrededor del punto de corte `cut_at`; cuenta como hecho solo si cubre ese punto.
        """
        if not self.can_stems() or not t.playable or not t.duration:
            return False
        missing = []
        for w in which:
            win = stems.window(self.settings, t.id, w)
            if w == "cut":
                if cut_at is None or stems.covers(win, cut_at - 5.0, min(t.duration, cut_at + 50.0)) and win.vocal:
                    continue
            elif win is not None:
                continue
            missing.append(w)
        if not missing:
            stems.touch(self.settings, t.id)
            return False
        self.jobs.submit("stems", f"{t.id}:{','.join(missing)}:{int(cut_at or 0)}", {
            "tid": t.id, "source": t.source, "stream_ref": t.stream_ref, "duration": t.duration,
            "which": missing, "cut_at": cut_at}, priority)
        return True

    def new_clip_name(self, plan_id: str) -> str:
        return f"{re.sub(r'[^A-Za-z0-9_-]', '', plan_id)[:24] or 'mix'}.wav"

    def clip_path(self, name: str) -> Path | None:
        if not CLIP_RE.match(name):
            return None
        p = self.settings.render_dir / name
        return p if p.is_file() else None

    def build_spec(self, plan: MixPlan, a: Track, b: Track, name: str) -> RenderSpec:
        r = plan.render
        assert r is not None
        st = r.get("stems_src")

        def deck(t: Track, key: str) -> DeckSpec:
            ref = StemsRef(**st[key]) if st else None
            return DeckSpec(id=t.id, path=str(audio.audio_path(self.settings, t.id, t.source, t.stream_ref)),
                            bpm=float(t.eff_bpm or 0), duration=float(t.duration), downbeats=list(t.downbeats),
                            trim=loudness_gain(t.loudness_lufs), stems=ref)

        return RenderSpec(
            a=deck(a, "a"), b=deck(b, "b"), tempo=r["tempo"], mult=r["mult"], a_ramp=r["a_ramp"], bars=r["bars"],
            b_ramp=r["b_ramp"], a_idx=r["a_idx"], b_idx=r["b_idx"], pitch_b=r["pitch_b"],
            lanes=[{"deck": l.deck, "param": l.param, "points": l.points} for l in plan.automation],
            out_name=name, kind=r.get("kind", "mix"))

    async def render(self, plan: MixPlan, a: Track, b: Track, name: str, deadline_s: float) -> dict[str, Any]:
        """Encarga el clip al worker y espera hasta `deadline_s` segundos. Lanza RenderError si falla o no llega."""
        spec = await asyncio.to_thread(self.build_spec, plan, a, b, name)
        job_id = self.jobs.submit("render", name, {"spec": spec.to_dict(), "out_dir": str(self.settings.render_dir), "lang": get_lang()},
                                  jobs_mod.P_RENDER)
        t0 = time.time()
        timeout = min(self.settings.render_timeout_s, max(1.0, deadline_s))
        try:
            while True:
                job = self.jobs.get(job_id)
                if job is None:
                    raise RenderError(tr("el trabajo de renderizado desapareció"))
                if job.status == jobs_mod.DONE and job.result:
                    log.info("Mezcla renderizada en %.1f s (worker %.1f s)", time.time() - t0,
                             job.result.get("render_s", 0))
                    self.cleanup()
                    return job.result
                if job.status in (jobs_mod.FAILED, jobs_mod.CANCELLED):
                    raise RenderError(job.error or tr("el renderizado falló"))
                if time.time() - t0 > timeout:
                    self.jobs.cancel(job_id)
                    raise RenderError(tr("el clip no estuvo listo en {n} s", n=f"{timeout:.0f}"))
                await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            self.jobs.cancel(job_id)  # solo se cancela si aún no había empezado
            raise

    def purge(self) -> None:
        """Borra todos los clips de mezcla (al terminar la sesión no sirven para nada)."""
        try:
            for p in [*self.settings.render_dir.glob("*.wav"), *self.settings.render_dir.glob("*.tmp")]:
                p.unlink(missing_ok=True)
        except OSError:
            log.exception("no se pudieron borrar los clips de mezcla")

    def cleanup(self) -> None:
        """Conserva solo los últimos clips (el actual y alguno anterior por si el navegador reintenta)."""
        keep = max(4, self.settings.render_keep)
        try:
            clips = sorted(self.settings.render_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
            for p in clips[keep:]:
                p.unlink(missing_ok=True)
            for p in self.settings.render_dir.glob("*.tmp"):
                if time.time() - p.stat().st_mtime > 600:
                    p.unlink(missing_ok=True)
        except OSError:
            log.exception("no se pudo limpiar la carpeta de clips")
