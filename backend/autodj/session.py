"""Sesión de DJ: cola del usuario, bucle de decisión, chat y registro de observabilidad.

La sesión no sabe nada de HTTP: recibe mensajes del cliente con `handle()` y emite
eventos con la función `send` que le pasan (la API la conecta al WebSocket).
"""

from __future__ import annotations

import asyncio
import copy
import difflib
import itertools
import logging
import math
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import jobs as jobs_mod, scoring, sources
from .agent import Choice, DJAgent, LLMError
from .agent.llm import explain
from .db import Database
from .i18n import set_lang, tr
from .library import Library
from .models import Track, TrackView
from .options import TransitionOption, describe, mmss, strategy_label, track_line, transition_options
from .render import RenderError
from .renderer import Renderer
from .strategies import STRATEGIES, MixPlan, RenderCtx, a_stem_window, cut_point, plan_transition

log = logging.getLogger(__name__)

Send = Callable[[dict[str, Any]], Awaitable[None]]

STYLES = {
    "subida": "subida progresiva",
    "olas": "olas de energía",
    "arriba": "mantener arriba",
}
LEAD_TIME = 8.0  # segundos mínimos entre el plan y la mezcla (descarga + estirado + decodificación)
URGENT_LEAD = 5.0
# Con mezcla adaptativa el worker renderiza el clip antes de que suene: hace falta más margen.
HISTORY_KEPT = 300  # canciones que se recuerdan de la sesión
HISTORY_SHOWN = 100  # y las que se envían a la interfaz
RENDER_LEAD = 18.0
RENDER_URGENT_LEAD = 10.0
RENDER_MIN_SLACK = 1.5  # si el clip llega con menos antelación que esto, se descarta y se usa el plan clásico
STEMS_REPLAN_MARGIN = 30.0
# Mensajes del navegador que solo tienen sentido con la sesión en marcha: tras «terminar» se ignoran.
_NEEDS_ACTIVE = {"chat", "track_started", "transition_started", "position", "plan_failed", "ended", "mix_now", "skip"}
_ids = itertools.count(1)


@dataclass
class Vibe:
    energy: float | None = None
    energy_delta: float = 0.0
    tempo_delta_pct: float = 0.0
    mood: float | None = None
    genres: list[str] = field(default_factory=list)
    avoid_genres: list[str] = field(default_factory=list)
    surprise: float | None = None
    tracks_left: int = 3


@dataclass
class Decision:
    """La siguiente transición decidida: canción, variante de mezcla y quién lo decidió.

    Se pide una vez por canción y se reutiliza mientras siga siendo válida (los replans automáticos, como
    la llegada de stems o el fin de un análisis, no la tiran ni la vuelven a pedir).
    """

    for_id: str  # la que sonaba al decidir
    target_id: str
    from_queue: bool
    plan: MixPlan | None  # la variante elegida (None en la primera canción)
    option_id: str
    reason: str
    by: str  # "ia" · "reserva" · "única"
    provider: str = ""  # "groq:openai/gpt-oss-120b"
    provider_label: str = ""
    latency_ms: int = 0
    why_fallback: str = ""
    score: float | None = None

    def badge(self) -> dict[str, Any]:
        """Lo que la interfaz muestra junto a la transición."""
        model = self.provider.split(":", 1)[1] if ":" in self.provider else ""
        return {"by": self.by, "provider": self.provider_label, "model": model, "latency_ms": self.latency_ms,
                "why": self.why_fallback, "option": self.option_id}


@dataclass
class PendingPlan:
    plan_id: str
    to_id: str
    from_queue: bool
    plan: MixPlan
    reason: str
    score: float | None
    decided: dict[str, Any] | None = None  # badge de la decisión (ver Decision.badge)


class Session:
    def __init__(self, library: Library, db: Database, agent: DJAgent, send: Send,
                 seed: int | None = None, clock: Callable[[], float] = time.monotonic,
                 renderer: Renderer | None = None):
        self.library, self.db, self.agent, self.send = library, db, agent, send
        self.renderer = renderer  # sin él, solo mezclas clásicas (el navegador estira y hace el EQ)
        self.clock = clock
        self.agent.log_fn = self._log
        self._fixed_seed = seed  # solo para reproducir sesiones (tests, depuración)
        self._busy: set[asyncio.Task] = set()  # start/chat en curso (esperan a la IA o a una descarga)
        self.reset(seed)

    def reset(self, seed: int | None = None) -> None:
        # Cancela cualquier plan o consulta a la IA en curso.
        for task in (getattr(self, "_plan_task", None), getattr(self, "_decide_task", None)):
            if task and not task.done():
                task.cancel()
        self.id = uuid.uuid4().hex[:12]
        self.seed = seed if seed is not None else random.randrange(1 << 30)
        self.rng = random.Random(self.seed)
        self.style = self.rng.choice(list(STYLES))
        self.active = False
        self.started_at = 0.0
        self.current: Track | None = None
        self.current_rate = 1.0
        self._pos = (0.0, 0.0)  # (posición en s, reloj) del último informe del cliente
        self.played: list[str] = []
        self.history: list[dict[str, Any]] = []  # lo que ha sonado, en orden: {"id", "ts", "via", "from_queue"}
        self.queue: list[str] = []
        self.pending: PendingPlan | None = None
        self.transitioning: PendingPlan | None = None
        self.waiting_first: str | None = None
        self.waiting_any = False  # sesión iniciada sin pistas listas: arranca con la primera que se analice
        self.vibes: list[Vibe] = []
        self.banned_tracks: set[str] = set()
        self.banned_artists: set[str] = set()
        self.failed: set[str] = set()
        self.preferred_strategy: str | None = None
        self.last_strategy: str | None = None
        self.logs: list[dict[str, Any]] = []
        self.chat_log: list[dict[str, Any]] = []
        self._plan_task: asyncio.Task | None = None
        self.decision: Decision | None = None
        self._decide_task: asyncio.Task | None = None
        self._decide_gen = 0
        self.last_decided: dict[str, Any] | None = None
        self._first_decided: dict[str, Any] | None = None
        self._llm_hint_sent = False
        self.render_bad: set[tuple[str, str]] = set()  # pares cuyo renderizado falló: no se reintenta
        self._stems_tried: set[tuple[str, str]] = set()
        self._stems_check_at = 0.0

    async def _emit(self, msg: dict[str, Any]) -> None:
        try:
            await self.send(msg)
        except Exception:  # noqa: BLE001
            log.exception("fallo enviando %s", msg.get("type"))

    async def _log(self, kind: str, title: str, detail: dict[str, Any] | None = None) -> None:
        entry = {"id": next(_ids), "ts": time.time(), "kind": kind, "title": title, "detail": detail or {}}
        self.logs = (self.logs + [entry])[-300:]
        await self._emit({"type": "log", "entry": entry})

    async def _say(self, text: str, role: str = "dj", via: str | None = None) -> None:
        entry = {"id": next(_ids), "ts": time.time(), "role": role, "text": text}
        if via:
            entry["via"] = via  # qué IA contestó (o "reserva"), para la interfaz
        self.chat_log = (self.chat_log + [entry])[-200:]
        await self._emit({"type": "chat", "message": entry})

    def snapshot(self) -> dict[str, Any]:
        def summ(tid: str | None) -> dict | None:
            t = self.library.get(tid) if tid else None
            return t.summary() if t else None

        nxt = self.transitioning or self.pending
        return {
            "type": "state",
            "active": self.active,
            "session_id": self.id,
            "style": tr(STYLES[self.style]),
            "llm": self.agent.enabled,
            "ai": {"enabled": self.agent.enabled, "providers": self.agent.status(), "last": self.last_decided},
            "now_playing": (dict(self.current.summary(), rate=self.current_rate) if self.current else None),
            "next": ({"track": summ(nxt.to_id), "strategy": nxt.plan.strategy,
                      "strategy_label": strategy_label(nxt.plan.strategy), "reason": nxt.reason, "from_queue": nxt.from_queue,
                      "in_transition": nxt is self.transitioning, "decided": nxt.decided} if nxt else None),
            "queue": [summ(t) or {"id": t, "title": "?"} for t in self.queue],
            "history": self._history_payload(summ),
            "targets": self._targets_dict(),
            "waiting_first": summ(self.waiting_first),
        }

    def _history_payload(self, summ: Callable[[str | None], dict | None]) -> list[dict[str, Any]]:
        """Canciones que ya han sonado en esta sesión, la más reciente primero (la que suena ahora no entra)."""
        out = []
        for h in reversed(self.history[:-1] if self.current and self.history and self.history[-1]["id"] == self.current.id
                          else self.history):
            s = summ(h["id"])
            if s:
                out.append(dict(s, played_at=h["ts"], via=strategy_label(h["strategy"]) if h["strategy"] else None,
                                from_queue=h["from_queue"],
                                decided=h.get("decided")))
            if len(out) >= HISTORY_SHOWN:
                break
        return out

    async def broadcast_state(self) -> None:
        await self._emit(self.snapshot())

    def _elapsed_min(self) -> float:
        return (self.clock() - self.started_at) / 60 if self.started_at else 0.0

    def targets(self) -> scoring.Targets:
        m = self._elapsed_min()
        if self.style == "subida":
            energy = 4.5 + 3.5 * min(1.0, m / 45)
        elif self.style == "olas":
            energy = 6.0 + 1.6 * math.sin(2 * math.pi * m / 20)
        else:
            energy = 7.2
        t = scoring.Targets(energy=energy)
        for v in self.vibes:
            if v.energy is not None:
                t.energy = v.energy
            t.energy += v.energy_delta
            t.bpm_shift += v.tempo_delta_pct / 100
            if v.mood is not None:
                t.mood = v.mood
            if v.genres:
                t.genres = list(v.genres)
            t.avoid_genres += v.avoid_genres
        t.energy = max(1.0, min(10.0, t.energy))
        return t

    def temperature(self) -> float:
        surprise = next((v.surprise for v in reversed(self.vibes) if v.surprise is not None), 0.25)
        return 0.04 + 0.3 * surprise

    def _targets_dict(self) -> dict[str, Any]:
        t = self.targets()
        return {"energy": round(t.energy, 1), "tempo_shift_pct": round(t.bpm_shift * 100, 1), "mood": t.mood,
                "genres": t.genres, "avoid_genres": t.avoid_genres, "preferred_strategy": self.preferred_strategy}

    def position(self) -> float:
        pos, at = self._pos
        return pos + (self.clock() - at) if at else pos

    def _ctx(self, pair: tuple[str, str] | None = None) -> RenderCtx:
        """Qué sabe hacer el sistema ahora mismo.

        `render` solo es cierto con worker vivo, la actual a su tempo y la pareja sin fallos previos.
        `shorten` (recortar las canciones tras el primer estribillo) vale también para la mezcla clásica.
        """
        shorten = bool(getattr(self.library.settings, "mix_shorten", True))
        if not self.renderer or abs(self.current_rate - 1.0) > 1e-3 or (pair is not None and pair in self.render_bad):
            return RenderCtx(render=False, shorten=shorten)
        ctx = self.renderer.ctx()
        ctx.shorten = shorten
        return ctx

    def _want_stems(self, cur: Track | None, target: Track | None) -> None:
        """Pide al worker los stems de lo que va a mezclarse (y de los primeros de la cola)."""
        r = self.renderer
        if not r or not r.can_stems():
            return
        if cur:
            self._request_exit_stems(cur, jobs_mod.P_NEAR)
        if target:
            r.request_stems(target, ("head",))
        for tid in self.queue[:3]:
            t = self.library.get(tid)
            if t and t.playable:
                r.request_stems(t, ("head",), jobs_mod.P_NEAR + 1)
                self._request_exit_stems(t, jobs_mod.P_NEAR + 1)

    def _request_exit_stems(self, t: Track, priority: int) -> None:
        """Stems del tramo por el que se saldrá de `t`: el del punto de corte si se recorta, y si no el final."""
        r = self.renderer
        assert r is not None
        cut = cut_point(TrackView(t)) if self._ctx().shorten else None
        if cut is not None:
            r.request_stems(t, ("cut",), priority, cut_at=cut)
        else:
            r.request_stems(t, ("tail",), priority)

    def _maybe_stems_replan(self) -> None:
        """Si ya hay stems de la pareja pendiente y la mezcla aún está lejos, se rehace con stems."""
        p, cur, r = self.pending, self.current, self.renderer
        if not (p and cur and r and not self.transitioning and r.can_stems()):
            return
        if (p.plan.render is None or p.plan.strategy in ("stems", "relay")
                or self.preferred_strategy not in (None, "stems", "relay")):
            return
        if p.plan.strategy not in ("bars", "eq", "quick", "fade"):
            return
        key = (cur.id, p.to_id)
        if key in self._stems_tried or key in self.render_bad:
            return
        if a_stem_window(self._ctx(), cur.id) is None or r.stems_window(p.to_id, "head") is None:
            return
        if p.plan.out_start - self.position() < RENDER_LEAD + STEMS_REPLAN_MARGIN:
            return
        self._stems_tried.add(key)
        self.replan("ya están los stems de la pareja", force_target=p.to_id)

    def _recent_artists(self) -> list[str]:
        arts = []
        for tid in self.played[-3:]:
            t = self.library.get(tid)
            if t:
                arts.append(t.artist)
        return arts

    def candidates(self, limit: int = 10) -> list[scoring.Candidate]:
        lib = self.library.playable()
        played = set(self.played)
        pairs = self.db.pair_counts(self.current.id) if self.current else {}
        kw = dict(recent_artists=self._recent_artists(), banned_tracks=self.banned_tracks,
                  banned_artists={a.lower() for a in self.banned_artists}, failed=self.failed)
        exclude_queue = set(self.queue)
        lib = [t for t in lib if t.id not in exclude_queue]
        ctx = self._ctx()
        cands = scoring.candidates(self.current, self.current_rate, lib, self.targets(), pairs,
                                   limit=limit, ctx=ctx, played=played, **kw)
        if not cands and played:  # biblioteca agotada: se permite repetir lo menos reciente
            cands = scoring.candidates(self.current, self.current_rate, lib, self.targets(), pairs,
                                       limit=limit, ctx=ctx, played=set(self.played[-8:]), **kw)
        if not self.current:
            openers = self.db.openers()
            for c in cands:
                c.score *= 1 / (1 + 0.5 * openers.get(c.track.id, 0))
            cands.sort(key=lambda c: c.score, reverse=True)
        return cands

    def _brief(self) -> str:
        """Estado de la sesión en pocas líneas de texto, para la IA (mucho más compacto que JSON)."""
        lines = []
        cur = self.current
        if cur:
            pos = self.position()
            left = max(0.0, cur.duration / max(self.current_rate, 1e-6) - pos)
            lines.append(f"Sonando: {track_line(cur)} · va por {mmss(pos)}, quedan {mmss(left)}")
        else:
            lines.append("Sonando: nada todavía (empieza la sesión)")
        recent = []
        for h in self.history[-5:-1] if cur else self.history[-4:]:
            t = self.library.get(h["id"])
            if t:
                recent.append(f"«{t.title}» — {t.artist}" + (f" (entró con {strategy_label(h['strategy'])})" if h.get("strategy") else ""))
        if recent:
            lines.append("Antes sonaron: " + "; ".join(recent))
        t = self._targets_dict()
        mood = t["mood"]
        mood_s = "sin preferencia" if mood is None else (f"oscuro ({mood:+.1f})" if mood < -0.15 else
                                                         f"luminoso ({mood:+.1f})" if mood > 0.15 else "neutro")
        obj = [f"energía {t['energy']:g}/10", f"ánimo {mood_s}"]
        if t["tempo_shift_pct"]:
            obj.append(f"tempo {t['tempo_shift_pct']:+g} %")
        if t["genres"]:
            obj.append("géneros pedidos: " + ", ".join(t["genres"]))
        if t["avoid_genres"]:
            obj.append("evitar: " + ", ".join(t["avoid_genres"]))
        lines.append("Objetivo ahora: " + " · ".join(obj) + f" · estilo de la sesión: {STYLES[self.style]}")
        if self.vibes:
            lines.append(f"Hay {len(self.vibes)} petición(es) de ambiente del usuario vigentes (ya incluidas en el objetivo).")
        if self.preferred_strategy:
            lines.append(f"El usuario prefiere la técnica: {strategy_label(self.preferred_strategy)}.")
        if self.queue:
            q = [f"«{x.title}» — {x.artist}" for x in (self.library.get(i) for i in self.queue[:5]) if x]
            lines.append("Cola del usuario (suena antes que tus elecciones, en este orden): " + "; ".join(q))
        if self.banned_artists or self.banned_tracks:
            lines.append(f"Vetados: {len(self.banned_tracks)} canciones; artistas: "
                         + (", ".join(sorted(self.banned_artists)) or "ninguno"))
        if self.last_strategy:
            lines.append(f"Última transición: {strategy_label(self.last_strategy)}")
        nxt = self.transitioning or self.pending
        if nxt:
            nt = self.library.get(nxt.to_id)
            if nt:
                lines.append(f"Siguiente ya decidida: «{nt.title}» con {strategy_label(nxt.plan.strategy)}"
                             + (" (ya está mezclando)" if nxt is self.transitioning else ""))
        return "\n".join(lines)

    async def start(self, mode: str = "auto", track_id: str | None = None, message: str | None = None) -> None:
        self.reset(self._fixed_seed)
        await self._emit({"type": "reset"})  # las pantallas conectadas empiezan de cero
        self.active = True
        self.started_at = self.clock()
        self.db.add_session(self.id, self.seed, self.style)
        await self._log("info", tr("Sesión iniciada · estilo: {style}", style=tr(STYLES[self.style])),
                        {"seed": self.seed, "ia": self.agent.enabled})
        if message:
            await self.chat(message)
        if mode == "upload" and track_id:
            t = self.library.get(track_id)
            if not t:
                await self._say(tr("No encuentro la pista que has adjuntado. Arranco yo con algo."))
            elif t.playable:
                await self._load_first(t, "petición del usuario")
                return
            elif t.status in ("pending", "analyzing"):
                self.waiting_first = t.id
                await self._say(tr("Analizando «{title}»… arranco en cuanto termine.", title=t.title))
                await self.broadcast_state()
                return
            else:
                await self._say(tr("No he podido analizar «{title}». Arranco yo con algo.", title=t.title))
        await self._choose_first()

    async def _choose_first(self) -> None:
        if self.queue:
            head = self.library.get(self.queue[0])
            if head and head.playable:
                self.queue.pop(0)
                self._first_decided = {"by": "usuario", "provider": "", "model": "", "latency_ms": 0, "why": "",
                                       "option": ""}
                await self._load_first(head, "petición del usuario")
                return
        pool = self.candidates()  # la reserva sortea entre todas estas
        cands = pool[:max(3, self.library.settings.ai_candidates + 1)]  # la IA ve las mejores
        if not cands:
            await self._say(tr("La biblioteca todavía no tiene pistas listas. Arranco en cuanto se analice la primera; "
                               "mientras, puedes adjuntar una tuya."))
            await self._log("info", tr("Sin pistas para empezar: esperando al análisis"),
                            {"library": len(self.library.all())})
            self.waiting_any = True
            await self.broadcast_state()
            return
        ids = [str(i + 1) for i in range(len(cands))]
        block = "\n".join(f"{i}: {track_line(c.track, score=c.score)}" for i, c in zip(ids, cands))
        choice, why = await self._ask_ai(self._brief(), block, ids, first=True)
        if choice:
            pick = cands[ids.index(choice.option)]
            reason = choice.reason or tr("buen arranque")
            d = Decision("", pick.track.id, False, None, choice.option, reason, "ia", choice.provider,
                         self.agent.provider_label(choice.provider), choice.latency_ms)
        else:
            mid = [c for c in pool if c.track.energy is None or 3.5 <= c.track.energy <= 7.5] or pool
            pick = scoring.weighted_pick(mid, self.temperature() * 2, self.rng)
            reason = tr("sorteo entre las de energía media")
            d = Decision("", pick.track.id, False, None, "", reason, "reserva", why_fallback=why)
        self._first_decided = self.last_decided = d.badge()
        await self._log("decision", tr("Primera canción: {title} · {by}", title=pick.track.title, by=self._by_text(d)),
                        {"reason": reason, "by": d.by, "provider": d.provider, "latency_ms": d.latency_ms,
                         "why_fallback": why, "options": block.splitlines(),
                         "chosen_option": choice.option if choice else None,
                         "chosen": pick.describe(), "candidates": [c.describe() for c in cands[:6]]})
        await self._load_first(pick.track, reason)

    def _by_text(self, d: Decision) -> str:
        if d.by == "ia":
            return tr("decide la IA ({provider})", provider=d.provider_label)
        if d.by == "única":
            return tr("única opción viable")
        return tr("modo reserva")

    async def _ask_ai(self, brief: str, block: str, ids: list[str], first: bool = False) -> tuple[Choice | None, str]:
        """Pregunta a la IA. Devuelve (elección, motivo del fallo si no la hay)."""
        if not self.agent.enabled:
            return None, tr("IA no configurada (añade una clave en .env)")
        try:
            choice = await self.agent.choose(brief, block, ids, first=first)
        except (LLMError, asyncio.TimeoutError) as e:
            await self._log("error", tr("La IA no pudo decidir: uso el modo reserva"),
                            {"error": str(e)[:300] or type(e).__name__})
            return None, explain(e)
        if choice.attempts:
            await self._log("info", tr("Respondió {provider} tras fallar otros", provider=self.agent.provider_label(choice.provider)),
                            {"fallos": choice.attempts})
        return choice, ""

    async def _load_first(self, t: Track, reason: str) -> None:
        self.waiting_first = None
        self.waiting_any = False
        self.db.add_transition(self.id, None, t.id, "start", None, reason == "petición del usuario", tr(reason))
        offset = 0.0
        await self._emit({"type": "load", "track": self._track_payload(t), "rate": 1.0, "offset": offset})
        await self.broadcast_state()

    def _track_payload(self, t: Track) -> dict[str, Any]:
        # Además del resumen, el navegador recibe la rejilla de la pista (compases, frases, intro y outro).
        return dict(t.summary(), loudness_lufs=t.loudness_lufs,
                    downbeats=[round(d, 3) for d in t.downbeats], phrase_offset=t.phrase_offset,
                    intro_end=t.intro_end, outro_start=t.outro_start)

    def replan(self, reason: str, urgent: bool = False, force_target: str | None = None,
               rethink: bool = False) -> None:
        """Rehace el plan de la siguiente transición.

        `rethink`: algo que importa a la decisión ha cambiado (canción nueva, cola, ambiente, vetos…) y hay
        que volver a preguntar a la IA. Sin él se reutiliza la decisión vigente: así los replans automáticos
        (stems listos, un análisis que termina, un clip que falla) no gastan llamadas ni las cancelan.
        """
        if not self.active or not self.current or self.transitioning:
            return
        if rethink:
            self._invalidate(reason)
        if self._plan_task and not self._plan_task.done():
            self._plan_task.cancel()  # solo el plan: la consulta a la IA sigue (está protegida con shield)
        self._plan_task = asyncio.create_task(self._plan(tr(reason), urgent, force_target))

    def _invalidate(self, why: str) -> None:
        self.decision = None
        self._decide_gen += 1
        if self._decide_task and not self._decide_task.done():
            self._decide_task.cancel()
            log.info("Descarto la decisión en curso: %s", why)
        self._decide_task = None

    async def wait_planned(self) -> None:
        for _ in range(3):
            task = self._plan_task
            if not task:
                return
            try:
                await task
            except asyncio.CancelledError:
                pass
            if task is self._plan_task:
                return

    def _decision_valid(self, d: Decision) -> bool:
        cur = self.current
        if not cur or d.for_id != cur.id:
            return False
        t = self.library.get(d.target_id)
        if not t or not t.playable or t.id in self.failed or t.id in self.banned_tracks:
            return False
        if t.artist and t.artist.lower() in self.banned_artists:
            return False
        head = self.library.get(self.queue[0]) if self.queue else None
        head_ok = bool(head and head.playable and head.id not in self.failed)
        if head_ok:
            return d.target_id == head.id  # type: ignore[union-attr]
        return not d.from_queue

    async def _ensure_decision(self, urgent: bool) -> Decision | None:
        d = self.decision
        if d and self._decision_valid(d):
            return d
        if self._decide_task is None or self._decide_task.done():
            self._decide_gen += 1
            self._decide_task = asyncio.create_task(self._decide(self._decide_gen, urgent))
        task = self._decide_task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():  # la decisión se invalidó (otro replan ya pedirá una nueva)
                return None
            raise  # se canceló este plan, no la decisión

    async def _decide(self, gen: int, urgent: bool) -> Decision | None:
        """Prepara las opciones (canción + variante de mezcla) y pide a la IA que elija una."""
        cur = self.current
        if not cur:
            return None
        st = self.library.settings
        head = self.library.get(self.queue[0]) if self.queue else None
        entries: list[tuple[Track, scoring.Candidate | None]] = []
        pool: list[scoring.Candidate] = []
        if head and head.playable and head.id not in self.failed:
            entries = [(head, None)]
        else:
            if head and not head.playable:
                await self._log("info", tr("«{title}» aún se está analizando; elijo otra mientras tanto", title=head.title))
            pool = self.candidates()
            entries = [(c.track, c) for c in pool[:max(1, st.ai_candidates)]]
        if not entries:
            await self._log("error", tr("No hay candidatas válidas"))
            return None
        from_queue = entries[0][1] is None
        view = TrackView(cur, self.current_rate)
        options: list[TransitionOption] = []
        blocks: list[str] = []
        for i, (t, c) in enumerate(entries, start=1):
            ctx = self._ctx((cur.id, t.id))
            lead = (RENDER_URGENT_LEAD if urgent else RENDER_LEAD) if ctx.render else (URGENT_LEAD if urgent else LEAD_TIME)
            plans = transition_options(view, t, self.position() + lead, urgent, self.rng, ctx,
                                       avoid=self.last_strategy, max_n=max(1, st.ai_variants),
                                       prefer=self.preferred_strategy)
            head_line = f"{i} {track_line(t, cur, c.score if c else None)}"
            if from_queue:
                head_line += tr("  ← primera de la cola del usuario: suena sí o sí, tú eliges cómo mezclarla")
            blocks.append(head_line)
            for j, p in enumerate(plans):
                oid = f"{i}{'abcdefgh'[j]}"
                text = describe(view, t, p, ctx)
                options.append(TransitionOption(oid, t, p, text))
                blocks.append(f"   {oid}: {text}")
        block = "\n".join(blocks)
        by_id = {o.id: o for o in options}
        if len(options) == 1:
            o = options[0]
            choice, why = None, ""
            d = Decision(cur.id, o.track.id, from_queue, o.plan, o.id,
                         tr("es la única forma viable de mezclarla") if from_queue else tr("es la única opción viable"),
                         "única")
        else:
            choice, why = await self._ask_ai(self._brief(), block, list(by_id))
            if choice:
                o = by_id[choice.option]
                d = Decision(cur.id, o.track.id, from_queue, o.plan, o.id, choice.reason or tr("encaja con el momento"),
                             "ia", choice.provider, self.agent.provider_label(choice.provider), choice.latency_ms)
            else:
                o = self._fallback_option(entries, options, from_queue, pool, view, urgent)
                d = Decision(cur.id, o.track.id, from_queue, o.plan, o.id,
                             tr("es la primera de tu cola") if from_queue else tr("sorteo ponderado entre las mejores"),
                             "reserva", why_fallback=why)
        cand = next((c for c in pool if c.track.id == d.target_id), None)
        d.score = cand.score if cand else None
        if gen != self._decide_gen or self.current is not cur:
            return None  # el mundo cambió mientras se pensaba
        self.decision = d
        self.last_decided = d.badge()
        title = (tr("Siguiente: {title}", title=o.track.title) + (tr(" (de tu cola)") if from_queue else "")
                 + f" · {self._by_text(d)}")
        await self._log("decision", title, {
            "by": d.by, "provider": d.provider, "latency_ms": d.latency_ms, "why_fallback": why,
            "reason": d.reason, "chosen_option": o.id, "chosen_text": o.text,
            "options": block.splitlines(),
            "chosen": cand.describe() if cand else o.track.summary(),
            "discarded": [c.describe() for t, c in entries if c and t.id != d.target_id][:5],
        })
        return d

    def _fallback_option(self, entries: list[tuple[Track, scoring.Candidate | None]],
                         options: list[TransitionOption], from_queue: bool, pool: list[scoring.Candidate],
                         view: TrackView, urgent: bool) -> TransitionOption:
        """Sin IA: la canción por sorteo ponderado entre todas las candidatas y, de sus variantes, la primera
        (ya vienen ordenadas por calidad)."""
        if from_queue or not pool:
            target = entries[0][0]
        else:
            pick = scoring.weighted_pick(pool, self.temperature(), self.rng)
            target = pick.track if pick else entries[0][0]
        mine = [o for o in options if o.track.id == target.id]
        if mine:
            return mine[0]
        # Sorteada fuera de las que vio la IA: se calcula su mejor variante.
        ctx = self._ctx((view.track.id, target.id))
        lead = (RENDER_URGENT_LEAD if urgent else RENDER_LEAD) if ctx.render else (URGENT_LEAD if urgent else LEAD_TIME)
        plan = transition_options(view, target, self.position() + lead, urgent, self.rng, ctx,
                                  avoid=self.last_strategy, max_n=1, prefer=self.preferred_strategy)[0]
        return TransitionOption("r", target, plan, describe(view, target, plan, ctx))

    async def _plan(self, why: str, urgent: bool, force_target: str | None = None) -> None:
        cur = self.current
        if not cur:
            return
        d = await self._ensure_decision(urgent)
        if d is None or self.current is not cur or self.transitioning:
            return
        target = self.library.get(d.target_id)
        if target is None:
            return
        self._want_stems(cur, target)
        ctx = self._ctx((cur.id, target.id))
        lead = (RENDER_URGENT_LEAD if urgent else RENDER_LEAD) if ctx.render else (URGENT_LEAD if urgent else LEAD_TIME)
        earliest = self.position() + lead
        plan, adjusted = self._plan_from_decision(d, cur, target, earliest, urgent, force_target, ctx)
        if adjusted:
            await self._log("info", tr("Ajusto la mezcla decidida ({why}); la canción sigue siendo la elegida", why=adjusted),
                            {"strategy": plan.strategy, "decidida": d.plan.strategy if d.plan else None})
        preferred = d.plan.strategy if d.plan else self.preferred_strategy
        if plan.render:
            plan = await self._render_plan(plan, cur, target, preferred, urgent)
        elif self.renderer and plan.strategy in ("fade", "bars", "eq", "stems"):
            await self._log("info", tr("Mezcla clásica: {why}", why=self._why_not_adaptive(cur, target, ctx)))
        if self.current is not cur or self.transitioning:
            return  # el mundo cambió mientras pensábamos
        badge = d.badge()
        self.pending = PendingPlan(uuid.uuid4().hex[:10], target.id, d.from_queue, plan, d.reason, d.score, badge)
        label = strategy_label(plan.strategy)
        await self._log("plan", tr("Mezcla: {label} en {at}s", label=label, at=f"{plan.out_start:.1f}"), {
            "strategy": plan.strategy, "notes": plan.notes, "out_start": round(plan.out_start, 2),
            "rendered": bool(plan.render), "clip_s": round(plan.duration, 1) if plan.render else None,
            "in_start": round(plan.in_start, 2), "rate_b": plan.rate_b, "bars": plan.bars,
            "swap_at": None if plan.swap_at is None else round(plan.swap_at, 2), "aligned": plan.aligned,
            "decided": badge, "why": why,
        })
        await self._emit({"type": "plan", "plan": dict(
            plan.to_dict(), plan_id=self.pending.plan_id, from_id=cur.id, label=label,
            track=self._track_payload(target), reason=d.reason, decided=badge)})
        await self.broadcast_state()

    def _plan_from_decision(self, d: Decision, cur: Track, target: Track, earliest: float, urgent: bool,
                            force_target: str | None, ctx: RenderCtx) -> tuple[MixPlan, str]:
        """El plan que se ejecuta: la variante elegida tal cual, salvo que ya no quepa.

        Devuelve (plan, motivo del ajuste o "" si es exactamente la elegida). Si hay que rehacerlo, se mantiene
        la canción y se intenta la misma estrategia; con stems recién llegados, se mejora a «Stems».
        """
        view = TrackView(cur, self.current_rate)
        chosen = d.plan
        if force_target and chosen is not None and chosen.strategy in ("bars", "eq", "quick", "fade"):
            p = STRATEGIES["stems"].plan(view, target, earliest, urgent, self.rng, ctx)
            if p is not None:
                return p, tr("ya están los stems: misma canción, mezcla con stems")
        if chosen is not None and not urgent and chosen.out_start >= earliest - 1e-6 and not force_target:
            return copy.deepcopy(chosen), ""
        reason = (tr("mezcla inmediata") if urgent else tr("ya no daba tiempo a la variante elegida")
                  if chosen is not None and chosen.out_start < earliest else tr("hay que rehacer el clip"))
        strategy = chosen.strategy if chosen is not None else self.preferred_strategy
        p = plan_transition(view, target, strategy, earliest, urgent=urgent, rng=self.rng,
                            avoid=self.last_strategy, ctx=ctx)
        return p, reason

    def _why_not_adaptive(self, cur: Track, target: Track, ctx: RenderCtx) -> str:
        """Motivo por el que esta pareja no se ha mezclado de forma adaptativa (para el registro)."""
        from . import adaptive

        if abs(self.current_rate - 1.0) > 1e-3:
            return tr("«{title}» suena estirada (x{rate}) por una mezcla clásica anterior; "
                      "la adaptativa necesita la pista a su tempo original", title=cur.title,
                      rate=f"{self.current_rate:.3f}")
        if (cur.id, target.id) in self.render_bad:
            return tr("el renderizado de esta pareja falló antes")
        if not ctx.render:
            caps = self.renderer.caps() if self.renderer else {}
            if not caps:
                return tr("no hay ningún worker vivo (¿está en marcha el servicio worker?)")
            missing = [k for k in ("rubberband", "scipy") if not caps.get(k)]
            if missing:
                return tr("al worker le falta {what}", what=tr(" y ").join(missing))
            return tr("la mezcla adaptativa está desactivada (MIX_ADAPTIVE)")
        return adaptive.explain(cur, target)

    async def _render_plan(self, plan: MixPlan, cur: Track, target: Track, preferred: str | None,
                           urgent: bool) -> MixPlan:
        """Encarga el clip al worker y devuelve el plan completo; si no se puede, el plan clásico."""
        assert self.renderer is not None and plan.render is not None
        name = self.renderer.new_clip_name(uuid.uuid4().hex[:10])
        deadline = plan.out_start - self.position() - RENDER_MIN_SLACK
        await self._log("info", tr("Preparando la mezcla ({label}, {bars} compases)…", label=strategy_label(plan.strategy),
                                   bars=plan.bars),
                        {"to": target.title, "notes": plan.notes})
        failure = ""
        try:
            res = await self.renderer.render(plan, cur, target, name, deadline)
        except RenderError as e:
            failure = str(e)
            self.render_bad.add((cur.id, target.id))
        else:
            if plan.out_start - self.position() < RENDER_MIN_SLACK or res["a_start"] - self.position() < RENDER_MIN_SLACK:
                failure = tr("el clip llegó tarde")
        if failure:
            await self._log("error", tr("No se pudo renderizar la mezcla: {why}; uso la mezcla clásica", why=failure[:160]),
                            {"to": target.title})
            lead = URGENT_LEAD if urgent else LEAD_TIME
            return plan_transition(TrackView(cur, self.current_rate), target, preferred,
                                   self.position() + lead, urgent=urgent, rng=self.rng, avoid=self.last_strategy,
                                   ctx=RenderCtx(render=False, shorten=self._ctx().shorten))
        plan.out_start, plan.in_start, plan.duration = res["a_start"], res["b_resume"], res["duration"]
        plan.render = dict(plan.render, file=name, url=f"/api/render/{name}", overlap_at=res["overlap_at"],
                           overlap_end=res["overlap_end"], maps=res["maps"], stems=bool(res.get("stems")),
                           grid_rms_ms=res.get("grid_rms_ms"), render_s=res.get("render_s"))
        plan.notes += "; stems" if res.get("stems") and "stems" not in plan.notes else ""
        return plan

    async def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind in ("start", "chat"):  # los lentos: si se termina la sesión mientras tanto, se cancelan
            task = asyncio.current_task()
            if task:
                self._busy.add(task)
            try:
                await self._handle(msg)
            finally:
                if task:
                    self._busy.discard(task)
        else:
            await self._handle(msg)

    async def stop(self) -> None:
        """Termina la sesión de verdad: deja el estado como recién creado.

        Se vacían la cola, el historial, el chat, los registros, las decisiones y los vetos; se cancela todo
        lo que estuviera en vuelo (un plan, una consulta a la IA, un chat que espera respuesta o una
        descarga de SoundCloud) y se borran los clips de mezcla renderizados. La biblioteca de pistas y
        la memoria de transiciones (para variar entre sesiones) se conservan.
        """
        me = asyncio.current_task()
        for task in list(self._busy):
            if task is not me:
                task.cancel()
        self._busy.clear()
        self.reset(self._fixed_seed)  # active=False y todo vacío; cancela plan y decisión
        if self.renderer:
            await asyncio.to_thread(self.renderer.purge)
        log.info("Sesión terminada: estado vaciado")
        await self._emit({"type": "reset"})
        await self.broadcast_state()

    async def _handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if not self.active and kind in _NEEDS_ACTIVE:
            return  # mensajes rezagados de una sesión que ya terminó
        if kind == "start":
            await self.start(msg.get("mode", "auto"), msg.get("track_id"), msg.get("message"))
        elif kind == "chat":
            await self.chat(msg.get("text") or "", msg.get("track_id"))
        elif kind == "track_started":
            await self._track_started(msg)
        elif kind == "transition_started":
            await self._transition_started(msg.get("plan_id"))
        elif kind == "position":
            if self.current and msg.get("track_id") == self.current.id:
                self._pos = (float(msg.get("position") or 0), self.clock())
                if self.renderer and self.clock() - self._stems_check_at > 5.0:
                    self._stems_check_at = self.clock()
                    self._maybe_stems_replan()
        elif kind == "plan_failed":
            await self._plan_failed(msg.get("plan_id"), msg.get("error") or "")
        elif kind == "queue_reorder":
            await self.reorder(list(msg.get("ids") or []))
        elif kind == "queue_remove":
            await self.queue_remove(msg.get("track_id"))
        elif kind == "queue_add":
            await self.queue_add(msg.get("track_id"), msg.get("position"))
        elif kind in ("mix_now", "skip"):
            await self._log("info", tr("Mezcla inmediata pedida desde la interfaz"))
            self.replan("el usuario pidió mezclar ya", urgent=True)
        elif kind == "ended":
            await self._ended()
        elif kind == "stop":
            await self.stop()
        elif kind == "lang":
            set_lang(msg.get("lang"))
            await self.broadcast_state()

    async def _track_started(self, msg: dict[str, Any]) -> None:
        t = self.library.get(msg.get("track_id") or "")
        if not t:
            return
        prev = self.current
        self.current = t
        self.current_rate = float(msg.get("rate") or 1.0)
        self._pos = (float(msg.get("offset") or 0.0), self.clock())
        if t.id not in self.played:
            self.played.append(t.id)
        if not self.history or self.history[-1]["id"] != t.id:
            trans = self.transitioning
            self.history = (self.history + [{
                "id": t.id, "ts": time.time(), "from_queue": bool(trans and trans.from_queue),
                "decided": trans.decided if trans else self._first_decided,
                "strategy": trans.plan.strategy if trans else None}])[-HISTORY_KEPT:]
        if self.transitioning:
            self.last_strategy = self.transitioning.plan.strategy
        self.transitioning = None
        self.pending = None
        if prev is not None:
            for v in self.vibes:
                v.tracks_left -= 1
            self.vibes = [v for v in self.vibes if v.tracks_left > 0]
        await self._log("info", tr("Suena: {title} — {artist}", title=t.title, artist=t.artist),
                        {"bpm": t.eff_bpm, "camelot": t.eff_camelot, "rate": self.current_rate})
        self.replan("empieza una canción nueva", rethink=True)
        await self.broadcast_state()

    async def _transition_started(self, plan_id: str | None) -> None:
        p = self.pending
        if not p or p.plan_id != plan_id:
            return
        self.transitioning, self.pending = p, None
        if self._plan_task and not self._plan_task.done():
            self._plan_task.cancel()
        # La pista entra en la mezcla: deja de ser reordenable y sale de la cola.
        if p.to_id in self.queue:
            self.queue.remove(p.to_id)
        self.db.add_transition(self.id, self.current.id if self.current else None, p.to_id,
                               p.plan.strategy, p.score, p.from_queue, p.reason)
        await self._log("info", tr("Transición en marcha"), {"to": p.to_id, "strategy": p.plan.strategy})
        await self.broadcast_state()

    async def _plan_failed(self, plan_id: str | None, error: str) -> None:
        p = self.pending
        if not p or p.plan_id != plan_id:
            return
        t = self.library.get(p.to_id)
        if error.startswith("clip:") and self.current:
            # El problema es del clip renderizado, no de la pista: se rehace con la mezcla clásica.
            self.render_bad.add((self.current.id, p.to_id))
            self.pending = None
            await self._log("error", tr("El navegador no pudo cargar el clip de la mezcla; uso la mezcla clásica"),
                            {"error": error[:200]})
            self.replan("falló el clip de la mezcla", force_target=p.to_id)
            return
        self.failed.add(p.to_id)
        self.pending = None
        if p.to_id in self.queue:
            self.queue.remove(p.to_id)
        await self._log("error", tr("No se pudo cargar {title}", title=t.title if t else p.to_id), {"error": error[:200]})
        await self._say(tr("No he podido cargar «{title}». Sigo con otra.", title=t.title if t else "?"))
        self.replan("falló la carga de la siguiente", rethink=True)

    async def _ended(self) -> None:
        """La pista terminó sin transición (p. ej. el plan llegó tarde): cargar la siguiente ya."""
        p = self.pending
        nxt = self.library.get(p.to_id) if p else None
        self.current, self.pending = None, None
        if nxt:
            if nxt.id in self.queue:
                self.queue.remove(nxt.id)
            await self._load_first(nxt, p.reason if p else "")
        else:
            await self._choose_first()

    async def queue_add(self, track_id: str | None, position: int | None = None) -> dict[str, Any]:
        t = self.library.get(track_id or "")
        if not t:
            return {"ok": False, "message": tr("no existe esa pista")}
        if t.status == "error":
            return {"ok": False, "message": tr("«{title}» no se pudo analizar", title=t.title)}
        if t.id in self.queue:
            return {"ok": True, "message": tr("«{title}» ya está en la cola", title=t.title),
                    "position": self.queue.index(t.id)}
        if self.current and t.id == self.current.id:
            return {"ok": False, "message": tr("«{title}» ya está sonando", title=t.title)}
        self.failed.discard(t.id)
        self.banned_tracks.discard(t.id)
        pos = len(self.queue) if position is None else max(0, min(int(position), len(self.queue)))
        self.queue.insert(pos, t.id)
        if not t.playable:
            self.library.prioritize(t.id)  # la pide el usuario: no espera a las del catálogo
        await self._log("info", tr("Añadida a la cola: {title}", title=t.title), {"position": pos})
        if self.current and pos == 0:
            self.replan("cambió la cabeza de la cola", rethink=True)
        await self.broadcast_state()
        ready = "" if t.playable else tr(" (en cuanto termine su análisis)")
        return {"ok": True, "message": tr("«{title}» en la posición {pos} de la cola{ready}", title=t.title, pos=pos + 1,
                                          ready=ready),
                "position": pos, "status": t.status}

    async def queue_remove(self, track_id: str | None) -> dict[str, Any]:
        if track_id not in self.queue:
            return {"ok": False, "message": tr("no está en la cola")}
        if self.transitioning and self.transitioning.to_id == track_id:
            return {"ok": False, "message": tr("ya está entrando en la mezcla")}
        was_head = self.queue[0] == track_id
        self.queue.remove(track_id)
        if was_head:
            self.replan("cambió la cabeza de la cola", rethink=True)
        await self.broadcast_state()
        return {"ok": True, "message": tr("quitada de la cola")}

    async def reorder(self, ids: list[str]) -> None:
        # Solo se reordena lo que sigue en la cola: lo que ya entró en la mezcla salió de ella.
        new = [i for i in dict.fromkeys(ids) if i in self.queue]
        new += [i for i in self.queue if i not in new]
        old_head = self.queue[0] if self.queue else None
        self.queue = new
        await self._log("info", tr("Cola reordenada"))
        if self.queue and self.queue[0] != old_head:
            self.replan("cambió la cabeza de la cola", rethink=True)
        await self.broadcast_state()

    async def library_changed(self, t: Track) -> None:
        await self._emit({"type": "library", "track": t.summary()})
        if not self.active:
            return
        if self.waiting_any and t.playable and not self.current:
            self.waiting_any = False
            await self._choose_first()
            return
        if self.waiting_first == t.id:
            if t.playable:
                await self._load_first(t, "petición del usuario")
            elif t.status == "error":
                self.waiting_first = None
                await self._say(tr("No he podido analizar «{title}». Arranco yo con algo.", title=t.title))
                await self._choose_first()
            return
        if t.id in self.queue and t.playable and self.queue[0] == t.id and self.current:
            nxt = self.pending
            if not nxt or nxt.to_id != t.id:
                await self._say(tr("«{title}» ya está analizada: suena a continuación.", title=t.title))
                self.replan("la pista pedida terminó de analizarse", rethink=True)

    async def chat(self, text: str, track_id: str | None = None) -> str:
        text = (text or "").strip()
        attached = self.library.get(track_id) if track_id else None
        shown = text or (f"📎 {attached.title}" if attached else "")
        if not shown:
            return ""
        await self._say(shown + (f"  📎 {attached.title}" if attached and text else ""), role="user")
        if not self.agent.enabled:
            # Sin IA: las pistas adjuntas se encolan igual; el aviso de configuración solo se da una vez.
            if attached:
                res = await self.queue_add(attached.id)
                reply = (tr("Hecho: {msg}.", msg=res["message"]) if res["ok"]
                         else tr("No he podido añadirla: {msg}.", msg=res["message"]))
            else:
                reply = tr("Ahora mismo no puedo entender mensajes de texto; sigo pinchando por mi cuenta.")
            if not self._llm_hint_sent:
                self._llm_hint_sent = True
                reply += tr(" (Para hablar conmigo por chat, añade una clave de IA en el archivo .env, por ejemplo GROQ_API_KEY.)")
            await self._say(reply)
            return reply
        if attached and not text:
            # Una pista adjunta sin más texto se encola directamente: no hace falta preguntar a la IA.
            res = await self.queue_add(attached.id)
            reply = (tr("Hecho: {msg}.", msg=res["message"]) if res["ok"]
                     else tr("No he podido añadirla: {msg}.", msg=res["message"]))
            await self._say(reply)
            return reply
        brief = self._brief()
        if attached:
            brief += (f"\nEl usuario acaba de adjuntar esta pista (id {attached.id}): {track_line(attached)}"
                      f" · estado del análisis: {attached.status}")
        via = None
        try:
            reply, provider = await self.agent.chat(text or "Pínchame esta.", brief, _SessionTools(self))
            via = self.agent.provider_label(provider) if provider else None
        except (LLMError, asyncio.TimeoutError) as e:
            why = str(e)[:300] or type(e).__name__
            log.warning("La IA no respondió al chat: %s", why)
            await self._log("error", tr("La IA no respondió al chat"), {"error": why})
            short = explain(e)
            via = "reserva"
            if attached:
                res = await self.queue_add(attached.id)
                reply = tr("La IA no responde ahora mismo ({why}), pero he añadido tu pista a la cola ({msg}).",
                           msg=res["message"], why=short)
            else:
                reply = tr("La IA no responde ahora mismo ({why}). La música sigue; prueba de nuevo en un rato.", why=short)
        await self._say(reply, via=via)
        return reply


class _SessionTools:
    """Herramientas que el agente puede usar en el chat."""

    def __init__(self, s: Session):
        self.s = s

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        fn = getattr(self, f"t_{name}", None)
        if not fn:
            return {"ok": False, "message": tr("herramienta desconocida: {name}", name=name)}
        return await fn(**{k: v for k, v in (args or {}).items()})

    async def t_search_library(self, query: str = "", limit: int = 5, **_: Any) -> dict[str, Any]:
        q = query.lower().strip()
        scored = []
        for t in self.s.library.all():
            if t.status == "error":
                continue
            hay = [t.title.lower(), t.artist.lower(), f"{t.artist} {t.title}".lower(), (t.eff_genre or "").lower()]
            r = max(difflib.SequenceMatcher(None, q, h).ratio() for h in hay)
            if q and any(q in h for h in hay):
                r = max(r, 0.9)
            scored.append((r, t))
        scored.sort(key=lambda x: x[0], reverse=True)
        hits = [dict(t.summary(), match=round(r, 2)) for r, t in scored[: max(1, min(10, int(limit)))] if r >= 0.45]
        return {"ok": True, "results": hits, "message": tr("{n} resultados", n=len(hits))}

    async def t_search_soundcloud(self, query: str = "", limit: int = 5, **_: Any) -> dict[str, Any]:
        """Busca en SoundCloud (solo pistas que se pueden reproducir). No añade nada."""
        if len(query.strip()) < 2:
            return {"ok": False, "message": tr("hace falta algo que buscar")}
        try:
            found = await asyncio.to_thread(sources.search_mainstream, query.strip(), max(1, min(8, int(limit))))
        except sources.MainstreamError as e:
            return {"ok": False, "message": str(e)}
        hits = []
        for r in found:
            have = self.s.library.get(r["id"])
            hits.append({"url": r["url"], "title": r["title"], "artist": r["artist"],
                         "duration": round(r["duration"]), "in_library": bool(have and have.status != "error"),
                         "id": r["id"]})
        return {"ok": True, "results": hits,
                "message": (tr("{n} resultados en SoundCloud", n=len(hits)) if hits
                            else tr("SoundCloud no devuelve nada reproducible"))}

    async def t_add_soundcloud(self, url: str = "", position: int | None = None, **_: Any) -> dict[str, Any]:
        """Añade a la biblioteca una pista de SoundCloud (por su enlace) y la pone en la cola."""
        try:
            meta = await asyncio.to_thread(sources.resolve_soundcloud_url, url.strip())
        except sources.MainstreamError as e:
            return {"ok": False, "message": str(e)}
        track = await asyncio.to_thread(self.s.library.add_soundcloud, meta)
        if not track:
            return {"ok": False, "message": tr("no se pudo añadir la pista")}
        res = await self.s.queue_add(track.id, position)
        res["track_id"] = track.id
        return res

    async def t_queue_add(self, track_id: str = "", position: int | None = None, **_: Any) -> dict[str, Any]:
        return await self.s.queue_add(track_id, position)

    async def t_queue_remove(self, track_id: str = "", **_: Any) -> dict[str, Any]:
        return await self.s.queue_remove(track_id)

    async def t_mix_now(self, **_: Any) -> dict[str, Any]:
        if not self.s.current:
            return {"ok": False, "message": tr("no suena nada todavía")}
        if self.s.transitioning:
            return {"ok": True, "message": tr("ya estoy mezclando")}
        self.s.replan("el usuario pidió mezclar ya", urgent=True)
        return {"ok": True, "message": tr("mezclo en el próximo inicio de frase")}

    async def t_set_vibe(self, tracks: int = 3, **kw: Any) -> dict[str, Any]:
        v = Vibe(tracks_left=max(1, int(tracks)))
        for k in ("energy", "mood", "surprise"):
            if kw.get(k) is not None:
                setattr(v, k, float(kw[k]))
        for k in ("energy_delta", "tempo_delta_pct"):
            if kw.get(k) is not None:
                setattr(v, k, float(kw[k]))
        v.genres = [str(g) for g in kw.get("genres") or []]
        v.avoid_genres = [str(g) for g in kw.get("avoid_genres") or []]
        self.s.vibes.append(v)
        if not (self.s.pending and self.s.pending.from_queue):
            self.s.replan("el usuario cambió el ambiente", rethink=True)
        await self.s.broadcast_state()
        return {"ok": True, "targets": self.s._targets_dict(), "message": tr("ambiente ajustado")}

    async def t_ban(self, kind: str = "track", value: str = "", **_: Any) -> dict[str, Any]:
        s = self.s
        if kind == "artist":
            s.banned_artists.add(value.lower())
            msg = tr("no pondré más a {value}", value=value)
        else:
            if not s.library.get(value):
                return {"ok": False, "message": tr("no existe esa pista (usa su id)")}
            s.banned_tracks.add(value)
            if value in s.queue:
                s.queue.remove(value)
            msg = tr("canción vetada")
        nxt = s.library.get(s.pending.to_id) if s.pending else None
        cur = s.current
        if cur and (cur.id in s.banned_tracks or cur.artist.lower() in s.banned_artists):
            s.replan("el usuario vetó lo que suena", urgent=True, rethink=True)
        elif nxt and (nxt.id in s.banned_tracks or nxt.artist.lower() in s.banned_artists):
            s.replan("el usuario vetó la siguiente", rethink=True)
        await s.broadcast_state()
        return {"ok": True, "message": msg}

    async def t_set_strategy(self, strategy: str = "auto", **_: Any) -> dict[str, Any]:
        self.s.preferred_strategy = None if strategy == "auto" else strategy
        self.s.replan("cambió la estrategia preferida", rethink=True)
        return {"ok": True, "message": tr("estrategia preferida: {strategy}", strategy=strategy)}

    async def t_library_overview(self, **_: Any) -> dict[str, Any]:
        lib = self.s.library.all()
        ready = [t for t in lib if t.playable]
        genres: dict[str, int] = {}
        for t in ready:
            g = t.eff_genre or "sin género"
            genres[g] = genres.get(g, 0) + 1
        bpms = [t.eff_bpm for t in ready if t.eff_bpm]
        return {"ok": True, "total": len(lib), "ready": len(ready), "genres": genres,
                "bpm_range": [round(min(bpms)), round(max(bpms))] if bpms else None,
                "analyzing": sum(1 for t in lib if t.status in ("pending", "analyzing"))}
