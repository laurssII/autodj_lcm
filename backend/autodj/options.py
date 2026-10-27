"""Variantes de transición que se le ofrecen a la IA.

El código sabe qué mezclas son posibles (tempo, tonalidad, compases, stems…); la IA decide cuál
conviene. Para cada canción candidata se preparan hasta N variantes viables y distintas entre sí
(estrategia, longitud y punto de la canción actual donde empieza) y se describen en palabras. La IA
elige una por su id y esa elección es la que se ejecuta.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, replace

from . import camelot
from .i18n import tr
from .models import Track, TrackView
from .strategies import LAST_RESORT, STRATEGIES, MixPlan, RenderCtx, cut_point, plan_transition, viable_strategies

PREFERRED_FIRST = ("stems", "relay", "wordplay")  # las más vistosas y limpias, cuando son posibles


@dataclass
class TransitionOption:
    id: str  # "2b": canción 2, variante b
    track: Track
    plan: MixPlan
    text: str  # descripción para la IA (y para el registro)

    @property
    def strategy(self) -> str:
        return self.plan.strategy


def mmss(s: float) -> str:
    s = max(0, int(round(s)))
    return f"{s // 60}:{s % 60:02d}"


def strategy_label(name: str) -> str:
    return tr(STRATEGIES[name].label if name in STRATEGIES else "Fundido")


def _where_in_a(a: TrackView, out_start: float, ctx: RenderCtx | None) -> str:
    """Dónde empieza la mezcla dentro de la canción que suena, en palabras."""
    bar = a.bar
    cut = cut_point(a)
    outro = a.outro_start
    if cut is not None and abs(out_start - cut) <= 4 * bar:
        where = tr("justo tras el primer estribillo de la que suena (la recorta)")
    elif outro is not None and out_start >= outro - 8 * bar:
        where = tr("en el outro de la que suena (la deja casi entera)")
    elif out_start >= 0.7 * a.duration:
        where = tr("en el tramo final de la que suena")
    else:
        where = tr("a mitad de la que suena (la recorta bastante)")
    return tr("{where}, a los {at}", where=where, at=mmss(out_start))


def _how_b_enters(b: Track, plan: MixPlan) -> str:
    if plan.aligned:
        return tr("la entrante llega a su cuerpo justo en el cambio de graves")
    if b.intro_end is not None and plan.in_start <= b.intro_end + 1:
        return tr("la entrante empieza por su intro")
    if plan.in_start < 1:
        return tr("la entrante empieza desde el principio")
    return tr("la entrante entra desde su {at}", at=mmss(plan.in_start))


def describe(a: TrackView, b: Track, plan: MixPlan, ctx: RenderCtx | None = None) -> str:
    label = strategy_label(plan.strategy)
    head = tr("{label} de {bars} compases", label=label, bars=plan.bars) if plan.bars else label
    notes = re.split(r" · (?:motivo|reason):", plan.notes or "")[0].strip()
    parts = [head]
    if notes:
        parts.append(notes[:140])
    parts.append(tr("empieza {where}", where=_where_in_a(a, plan.out_start, ctx)))
    parts.append(_how_b_enters(b, plan))
    if abs(plan.rate_b - 1.0) > 0.005:
        parts.append(tr("tempo de la entrante ajustado un {pct} %", pct=f"{abs(plan.rate_b - 1) * 100:.1f}"))
    parts.append(tr("dura unos {n} s", n=max(1, round(plan.duration))))
    return "; ".join(parts)


def transition_options(a: TrackView, b: Track, earliest: float, urgent: bool, rng: random.Random,
                       ctx: RenderCtx | None, avoid: str | None = None, max_n: int = 3,
                       prefer: str | None = None) -> list[MixPlan]:
    """Hasta `max_n` planes viables y distintos para pasar de `a` a `b`.

    Primero una variante por estrategia que encaje (las de stems y el wordplay delante, la usada en la
    transición anterior al final); después, si hay hueco, la primera estrategia con el otro punto de salida
    (recortando tras el estribillo o dejando la canción hasta el outro). Si ninguna mezcla de verdad encaja,
    un único plan de último recurso (mezcla corta, corte con eco o fundido).
    """
    viable = [n for n in viable_strategies(a.track, b, a.rate, ctx) if n not in LAST_RESORT]
    first = ([prefer] if prefer in viable else []) + [n for n in PREFERRED_FIRST if n in viable and n != prefer]
    rest = [n for n in viable if n not in first]
    rng.shuffle(rest)
    order = first + rest
    if avoid in order and len(order) > 1 and avoid != prefer:
        order.remove(avoid)
        order.append(avoid)
    plans: list[MixPlan] = []
    seen: set[tuple[str, int]] = set()

    def add(p: MixPlan | None) -> None:
        if p is None:
            return
        key = (p.strategy, int(round(p.out_start / max(1.0, a.bar * 4))))
        if key in seen:
            return
        seen.add(key)
        plans.append(p)

    for name in order:
        if len(plans) >= max_n:
            break
        add(STRATEGIES[name].plan(a, b, earliest, urgent, rng, ctx))
    if plans and len(plans) < max_n and ctx is not None and not urgent:
        alt = replace(ctx, shorten=not ctx.shorten)
        base = plans[0]
        p = STRATEGIES[base.strategy].plan(a, b, earliest, urgent, rng, alt) if base.strategy in STRATEGIES else None
        if p is not None and abs(p.out_start - base.out_start) > 8 * a.bar:
            add(p)
    if not plans:
        plans.append(plan_transition(a, b, None, earliest, urgent, rng, avoid, ctx))
    return plans[:max_n]


def track_line(t: Track, current: Track | None = None, score: float | None = None) -> str:
    """Una canción en una línea, con lo que importa para decidir."""
    s = t.summary()
    bits = [f"«{s['title']}» — {s['artist'] or tr('artista desconocido')}"]
    if s["bpm"]:
        bits.append(f"{s['bpm']:g} BPM")
    if s["camelot"]:
        key = s["camelot"]
        if current is not None and current.eff_camelot:
            h = camelot.harmonic_score(current.eff_camelot, t.eff_camelot, current.key_conf, t.key_conf)
            key += tr(" (armonía {h})", h=f"{h:.2f}")
        bits.append(key)
    if s["energy"] is not None:
        bits.append(tr("energía {n}", n=f"{s['energy']:g}"))
    if s["mood"] is not None:
        bits.append(tr("ánimo {n}", n=f"{s['mood']:+.1f}"))
    if s["genre"]:
        bits.append(str(s["genre"]))
    if score is not None:
        bits.append(tr("puntuación {n}", n=f"{score:.2f}"))
    return " · ".join(bits)
