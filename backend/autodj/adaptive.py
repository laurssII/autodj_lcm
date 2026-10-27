"""Planificador de mezclas adaptativas: decide cómo encajar dos pistas aunque no coincidan.

Parte de las estrategias de compases, EQ y stems (que aportan las curvas de automatización) y añade
tempo común con rampas, desplazamiento de tonalidad y elección de los puntos de salida y entrada.

Son funciones puras sobre `Track`/`TrackView`; el audio lo produce `render.py` en el worker. El resultado
es un `MixPlan` con el campo `render` relleno.
"""

from __future__ import annotations

import bisect
import math
import random
from typing import TYPE_CHECKING, Any

from . import adapt, camelot
from .i18n import tr
from .models import Track, TrackView
from .render import CTX, estimate_duration, grid_regular

if TYPE_CHECKING:  # pragma: no cover
    from .strategies import MixPlan, RenderCtx, Strategy, StemWindow

MIN_DOWNBEATS = 16
# Armonía mínima (tras el posible desplazamiento) según la estrategia.
MIN_HARMONY = {"bars": 0.6, "stems": 0.6, "eq": 0.0, "quick": 0.0, "wordplay": 0.4, "relay": 0.85}
VOICE_ON = 0.3  # actividad de voz (0–1) a partir de la cual se considera que se canta
RELAY_LEAD_IN = 2  # compases que puede tardar la saliente en empezar a cantar tras el inicio de la mezcla
CUT_MIN_SECONDS = 5.0  # lo que dura como mínimo el clip de un corte con eco (el eco necesita su cola)
WORD_TOLERANCE = 0.09  # s de diferencia máxima entre la palabra de A y la de B dentro del compás
MAX_A_TRIES = 60  # inicios distintos que se prueban en la saliente antes de rendirse con este tamaño
B_TAIL_SAFETY = 10.0  # s que deben quedar de la entrante tras el clip
SPAN_PAD = CTX + 0.25


def _harmony(a: Track, b: Track) -> tuple[adapt.KeyPlan, float]:
    kp = adapt.key_plan(a.eff_camelot, b.eff_camelot, a.key_conf, b.key_conf)
    return kp, (kp.score if kp.semitones else kp.base)


def explain(a: Track, b: Track) -> str:
    """Por qué dos pistas no admiten mezcla adaptativa (para el registro de la sesión)."""
    if len(a.downbeats) < MIN_DOWNBEATS or len(b.downbeats) < MIN_DOWNBEATS:
        return tr("no se detectaron compases suficientes en una de las dos")
    if adapt.tempo_plan(a.eff_bpm, b.eff_bpm) is None:
        ba, bb = a.eff_bpm or 0, b.eff_bpm or 0
        gap = min(abs(bb * m / ba - 1) for m in (0.5, 1.0, 2.0)) * 100 if ba else 0
        return tr("tempo demasiado distinto ({a} vs {b} BPM, {gap} % de diferencia; máximo {max} %)", a=f"{ba:.0f}", b=f"{bb:.0f}",
                  gap=f"{gap:.0f}", max=f"{200 * adapt.MAX_DEV:.0f}")
    kp, h = _harmony(a, b)
    if h < MIN_HARMONY["bars"]:
        return tr("tonalidades que chocan ({ka} → {kb}, fiabilidad {ca}/{cb}) y no se arreglan con ±{n} semitonos",
                  ka=a.eff_camelot, kb=b.eff_camelot, ca=f"{a.key_conf:.2f}", cb=f"{b.key_conf:.2f}", n=adapt.MAX_SHIFT)
    return tr("no cabe una mezcla en los puntos de entrada y salida disponibles")


def viable(strategy: "Strategy", a: Track, b: Track) -> bool:
    """¿Se pueden encajar `a` y `b` con una mezcla adaptativa de esta estrategia?"""
    if len(a.downbeats) < MIN_DOWNBEATS or len(b.downbeats) < MIN_DOWNBEATS:
        return False
    if adapt.tempo_plan(a.eff_bpm, b.eff_bpm) is None:
        return False
    _, h = _harmony(a, b)
    return h >= MIN_HARMONY.get(strategy.name, 0.6)


def _nearest(values: list[float], t: float) -> int:
    return min(range(len(values)), key=lambda i: abs(values[i] - t))


def b_grid_ok(b: Track, idx: int, b_len: float) -> bool:
    return grid_regular(b.downbeats, idx, int(math.ceil(idx + b_len)))


def _entry(b: Track, bars: int, mult: float, b_ramp: int, aligned_only: bool) -> tuple[int, bool] | None:
    """Índice del downbeat de B donde entra. Devuelve (índice, ¿ajustado a su intro?).

    Ajustado: la entrante entra medio solapamiento antes de que acabe su intro, así el
    intercambio de graves cae cuando empieza su cuerpo. `mult` convierte compases de salida
    en compases de la entrante (con doble tiempo, uno de B ocupa dos de salida).
    """
    db = b.downbeats
    half_b = max(1, int(round((bars / 2) / mult)))
    b_len = (bars + b_ramp) / mult  # compases de B que consume el clip
    bar_b = 240.0 / (b.eff_bpm or 120.0)

    def fits(idx: int) -> bool:
        return (idx >= 0 and idx + b_len + 1 < len(db) and db[idx] + b_len * bar_b < b.duration - B_TAIL_SAFETY
                and b_grid_ok(b, idx, b_len))

    ie = b.intro_end
    if ie is not None:
        i = _nearest(db, ie)
        if abs(db[i] - ie) < bar_b / 2 and i - half_b >= 0 and fits(i - half_b):
            return i - half_b, True
    if aligned_only:
        return None
    # Sin intro aprovechable: la primera frase de 8 compases desde la que cabe todo el clip.
    start = b.phrase_offset % 8
    for idx in range(start, len(db), 8):
        if idx + b_len * 1.0 > len(db) * 0.6:
            break
        if fits(idx):
            return idx, False
    return None


def _covered(win: "StemWindow | None", t0: float, t1: float, duration: float) -> bool:
    """¿La ventana de stems cubre el tramo (con su contexto)? Fuera de la pista no hace falta audio."""
    if win is None:
        return False
    t0, t1 = max(0.0, t0), min(duration, t1)
    return t0 >= win.start - 1e-3 and t1 <= win.end + 1e-3


def plan_rendered(strategy: "Strategy", a: TrackView, b: Track, earliest: float, urgent: bool,
                  rng: random.Random, ctx: "RenderCtx") -> "MixPlan | None":
    """Plan de mezcla adaptativa con las curvas de `strategy`, o None si no hay sitio."""
    from .strategies import MixPlan, a_stem_window, choose_out_start, mix_ideal  # noqa: PLC0415 (ciclo de importación)

    ta = a.track
    if abs(a.rate - 1.0) > 1e-3 or not viable(strategy, ta, b):
        return None
    tp = adapt.tempo_plan(ta.eff_bpm, b.eff_bpm)
    assert tp is not None
    kp, harmony = _harmony(ta, b)
    pitch_b = kp.semitones

    a_ramp = adapt.ramp_bars(tp.dev_a, urgent)
    b_ramp = adapt.ramp_bars(tp.dev_b, urgent)
    if pitch_b and b_ramp == 0:
        b_ramp = 4
    bar_a = 240.0 / ta.eff_bpm  # type: ignore[operator]
    bar_t = 240.0 / tp.tempo
    use_stems = strategy.name in ("stems", "relay")
    win_a = a_stem_window(ctx, ta.id) if use_stems else None
    win_b = ctx.stems(b.id, "head") if use_stems and ctx.stems else None
    if use_stems and (win_a is None or win_b is None):
        return None

    options = strategy._options_h(harmony, rng, urgent)  # type: ignore[attr-defined]
    if ctx.shorten:
        options = [o for o in options if o <= 16] or [8]
    for aligned_only in (True, False):
        for bars in options:
            if not aligned_only and bars > 16:
                continue
            found = _entry(b, bars, tp.mult, b_ramp, aligned_only)
            if found is None:
                continue
            b_idx, aligned = found
            a_len = bars * bar_a
            lo = earliest + a_ramp * bar_a
            if win_a is not None:
                lo = max(lo, win_a.start + a_ramp * bar_a + SPAN_PAD + 0.5)
            ideal = mix_ideal(a, (bars // 2) * bar_a, ctx, earliest, lead=a_ramp * bar_a)
            # Se prueban varios inicios en A: los que caen en un tramo de compases irregulares se descartan.
            a_idx, ia0, skip = -1, -1, set()
            for _ in range(MAX_A_TRIES):
                ov = choose_out_start(a, lo, a_len, ideal, urgent, skip)
                if ov is None:
                    break
                skip.add(ov)
                i = _nearest(ta.downbeats, ov)
                j0 = i - a_ramp
                if j0 < 0 or ta.downbeats[j0] < earliest - 0.05 or i + bars >= len(ta.downbeats):
                    continue
                if grid_regular(ta.downbeats, j0, i + bars):
                    a_idx, ia0 = i, j0
                    break
            if a_idx < 0:
                continue
            b_len = (bars + b_ramp) / tp.mult
            if b_idx + int(b_len) + 1 >= len(b.downbeats):
                continue
            if use_stems:
                a_end = ta.downbeats[a_idx + bars]
                if not _covered(win_a, ta.downbeats[ia0] - SPAN_PAD, a_end + SPAN_PAD, ta.duration):
                    continue
                b_end = b.downbeats[b_idx] + b_len * 240.0 / b.eff_bpm  # type: ignore[operator]
                if not _covered(win_b, b.downbeats[b_idx] - SPAN_PAD, b_end + SPAN_PAD, b.duration):
                    continue
            extra: dict[str, float] = {}
            if getattr(strategy, "relay", False):
                pts = relay_points(ta, b, tp, win_a, win_b, a_idx, b_idx, bars)
                if pts is None:
                    continue
                extra = {"v_end": pts[0], "v_in": pts[1]}
            return _make(strategy, ta, b, tp, kp, harmony, bars, bar_t, a_ramp, b_ramp, a_idx, b_idx,
                         aligned, win_a, win_b, extra)
    return None


def _voice(win: "StemWindow", t: float) -> bool | None:
    """¿Se canta en el instante `t` (s de la pista)? None si la ventana no llega ahí o no hay datos de voz."""
    if not win.vocal:
        return None
    i = int((t - win.start) / win.step)
    if not 0 <= i < len(win.vocal):
        return None
    return win.vocal[i] >= VOICE_ON


def relay_points(ta: Track, tb: Track, tp: adapt.TempoPlan, win_a: "StemWindow | None", win_b: "StemWindow | None",
                 a_idx: int, b_idx: int, bars: int) -> tuple[float, float] | None:
    """Dónde se pasan la voz las dos pistas en un relevo de voz: (fin de la voz de A, entrada de la voz de B),
    en segundos desde el inicio del solapamiento. None si no hay relevo posible.

    - La saliente tiene que cantar casi desde el principio y durante al menos un tercio de la mezcla;
      la voz se queda hasta el final de esa frase (se aguantan respiros de hasta un compás).
    - La entrante no puede cantar antes de que acabe la saliente; su voz entra cuando le toca por su propia
      canción (o al final de la mezcla si aún no canta).
    """
    if win_a is None or win_b is None or not win_a.vocal or not win_b.vocal:
        return None
    bar_a, bar_b = 240.0 / ta.eff_bpm, 240.0 / tb.eff_bpm  # type: ignore[operator]
    bar_t = 240.0 / tp.tempo
    length = bars * bar_t
    half = bar_t / 2
    n = int(length / half)
    ta0, tb0 = ta.downbeats[a_idx], tb.downbeats[b_idx]
    # tiempo de salida u → instante de cada pista (a tempo común durante el solapamiento)
    a_at = lambda u: ta0 + u * bar_a / bar_t  # noqa: E731
    b_at = lambda u: tb0 + u * bar_b / (tp.mult * bar_t)  # noqa: E731

    act = [_voice(win_a, a_at(k * half)) for k in range(n)]
    if None in act:
        return None
    first = next((k for k, v in enumerate(act) if v), None)
    if first is None or first > RELAY_LEAD_IN * 2:
        return None
    k, gap = first, 0
    end = first
    while k < n and gap <= 2:  # respiros de hasta un compás (2 medios compases)
        if act[k]:
            end, gap = k + 1, 0
        else:
            gap += 1
        k += 1
    v_end = math.ceil(end * half / bar_t) * bar_t  # al siguiente compás
    if v_end < length / 3 or v_end > length * 0.85:
        return None

    onset: float | None = None
    for j in range(int((length + 4 * bar_t) / half)):
        v = _voice(win_b, b_at(j * half))
        if v is None:
            return None  # la ventana de B no llega hasta aquí
        if v and _voice(win_b, b_at((j + 1) * half)):  # dos pasos seguidos: no es un ruido suelto
            onset = j * half
            break
    if onset is not None and onset < v_end - 0.5 * bar_t:
        return None  # B canta antes de que A acabe
    v_in = min(length - bar_t, max(v_end, onset if onset is not None else length))
    return round(v_end, 4), round(v_in, 4)


def _make(strategy: "Strategy", ta: Track, tb: Track, tp: adapt.TempoPlan, kp: adapt.KeyPlan,
          harmony: float, bars: int, bar_t: float, a_ramp: int, b_ramp: int, a_idx: int, b_idx: int,
          aligned: bool, win_a: "StemWindow | None", win_b: "StemWindow | None",
          extra: dict[str, float] | None = None) -> "MixPlan":
    from .strategies import MixPlan, StemsMix  # noqa: PLC0415

    length = bars * bar_t
    if isinstance(strategy, StemsMix):
        built = strategy._build(0.0, 0.0, length, 1.0, bars, bar_t, aligned, harmony, **(extra or {}))
    else:
        built = strategy._build(0.0, 0.0, length, 1.0, bars, bar_t, aligned)  # type: ignore[attr-defined]

    duration = estimate_duration(ta.eff_bpm, tp.tempo, tb.eff_bpm * tp.mult, a_ramp, bars, b_ramp)  # type: ignore[operator]
    out_start = ta.downbeats[a_idx - a_ramp]
    b_resume = tb.downbeats[min(len(tb.downbeats) - 1, b_idx + int(round((bars + b_ramp) / tp.mult)))]

    bits = []
    if abs(tp.dev_a) >= adapt.RAMP_MIN_DEV or abs(tp.dev_b) >= adapt.RAMP_MIN_DEV:
        bits.append(tr("tempo común {bpm} BPM", bpm=f"{tp.tempo:.1f}") + (tr(" (doble/mitad)") if tp.mult != 1 else ""))
    if kp.semitones:
        bits.append(tr("entrante {n} semitonos durante la mezcla", n=f"{kp.semitones:+d}"))
    note = built.notes + ("; " + ", ".join(bits) if bits else "")
    render: dict[str, Any] = {
        "a_id": ta.id, "b_id": tb.id,
        "tempo": round(tp.tempo, 4), "mult": tp.mult,
        "a_ramp": a_ramp, "bars": bars, "b_ramp": b_ramp,
        "a_idx": a_idx, "b_idx": b_idx, "pitch_b": kp.semitones,
        "dev_a": round(tp.dev_a, 4), "dev_b": round(tp.dev_b, 4),
        "stems_src": None if win_a is None or win_b is None else {
            "a": {"dir": win_a.dir, "window": win_a.window, "start": win_a.start, "end": win_a.end},
            "b": {"dir": win_b.dir, "window": win_b.window, "start": win_b.start, "end": win_b.end},
        },
    }
    return MixPlan(strategy.name, out_start, b_resume, duration, 1.0, bars, built.automation, note,
                   built.swap_at, aligned, render)


def plan_cut(strategy: "Strategy", a: TrackView, b: Track, earliest: float, urgent: bool, rng: random.Random,
             ctx: "RenderCtx") -> "MixPlan | None":
    """Corte en un inicio de frase de `a`: su eco sigue sonando y `b` arranca ahí desde su primer compás."""
    from .strategies import Lane, MixPlan, choose_out_start, mix_ideal  # noqa: PLC0415

    ta = a.track
    if len(ta.downbeats) < MIN_DOWNBEATS or len(b.downbeats) < MIN_DOWNBEATS or not (ta.eff_bpm and b.eff_bpm):
        return None
    ideal = mix_ideal(a, 0.0, ctx, earliest)
    cut, skip = None, set()
    for _ in range(6):
        cut = choose_out_start(a, earliest, 0.0, ideal, urgent, skip)
        if cut is None:
            return None
        i = _nearest(ta.downbeats, cut)
        if grid_regular(ta.downbeats, i - 4, i + 2):
            break
        skip.add(cut)
    else:
        cut = choose_out_start(a, earliest, 0.0, ideal, urgent)  # ninguno regular: el mejor sin más
        if cut is None:
            return None
    a_idx = _nearest(ta.downbeats, cut)
    bar_b = 240.0 / b.eff_bpm
    bars = max(2, int(math.ceil(CUT_MIN_SECONDS / bar_b)))
    # B arranca en su primer compás útil (el primero de la rejilla) y suena `bars` compases dentro del clip
    b_idx = 0
    duration = bars * bar_b
    if bars + 2 >= len(b.downbeats) or b.downbeats[b_idx] + duration > b.duration - B_TAIL_SAFETY:
        return None
    from .strategies import Lane as _Lane  # noqa: PLC0415
    lanes = [_Lane("A", "gain", [[0.0, 1.0], [0.01, 0.0]]), _Lane("B", "gain", [[0.0, 1.0]])]
    render: dict[str, Any] = {
        "kind": "echo_cut", "a_id": ta.id, "b_id": b.id, "tempo": round(ta.eff_bpm, 4), "mult": 1.0,
        "a_ramp": 0, "bars": bars, "b_ramp": 0, "a_idx": a_idx, "b_idx": b_idx, "pitch_b": 0,
        "dev_a": 0.0, "dev_b": 0.0, "stems_src": None, "handoff": 0.005,
    }
    note = tr("corte sobre el compás: la saliente se va con eco y la entrante arranca a su propio tempo")
    return MixPlan(strategy.name, ta.downbeats[a_idx], b.downbeats[b_idx] + duration, duration, 1.0, bars, lanes,
                   note, 0.0, True, render)


def _bar_pos(downbeats: list[float], t: float) -> float | None:
    """Posición de `t` en compases (índice fraccionario de la rejilla); None si cae fuera de ella."""
    i = bisect.bisect_right(downbeats, t) - 1
    if i < 0 or i + 1 >= len(downbeats):
        return None
    return i + (t - downbeats[i]) / (downbeats[i + 1] - downbeats[i])


def plan_wordplay(strategy: "Strategy", a: TrackView, b: Track, earliest: float, urgent: bool,
                  rng: random.Random, ctx: "RenderCtx") -> "MixPlan | None":
    """Enlace de palabra dentro de una mezcla por EQ.

    La mezcla es de 8 compases y la palabra cae en su mitad. Entre todas las palabras que A y B comparten
    se elige la que, con las pistas ya a compás, cae en el mismo punto del compás en las dos (diferencia
    de pocas decenas de ms), cerca del punto de corte y en un inicio de frase.
    """
    from .strategies import MixPlan, mix_ideal  # noqa: PLC0415

    ta = a.track
    tp = adapt.tempo_plan(ta.eff_bpm, b.eff_bpm)
    if tp is None or len(ta.downbeats) < MIN_DOWNBEATS or len(b.downbeats) < MIN_DOWNBEATS:
        return None
    kp, harmony = _harmony(ta, b)
    if harmony < MIN_HARMONY["wordplay"]:
        return None
    a_ramp = adapt.ramp_bars(tp.dev_a, urgent)
    b_ramp = adapt.ramp_bars(tp.dev_b, urgent)
    if kp.semitones and b_ramp == 0:
        b_ramp = 4
    bars = 8
    bar_a = 240.0 / ta.eff_bpm  # type: ignore[operator]
    bar_b = 240.0 / b.eff_bpm  # type: ignore[operator]
    bar_t = 240.0 / tp.tempo
    ideal = mix_ideal(a, 0.0, ctx, earliest, lead=a_ramp * bar_a)
    b_len = (bars + b_ramp) / tp.mult
    best: tuple[float, Any] | None = None
    for wa, wb in strategy._pairs(a.words(), b):  # type: ignore[attr-defined]
        pa = _bar_pos(ta.downbeats, wa.start)
        pb = _bar_pos(b.downbeats, wb.start)
        if pa is None or pb is None:
            continue
        a_idx = int(round(pa - bars / 2))
        ka = pa - a_idx  # compases de salida desde el inicio del solapamiento hasta la palabra
        b_exact = pb - ka / tp.mult
        b_idx = int(round(b_exact))
        err = abs(b_exact - b_idx) * tp.mult * bar_t  # s de desfase entre las dos palabras en la mezcla
        if err > WORD_TOLERANCE or a_idx - a_ramp < 0 or a_idx + bars + 1 >= len(ta.downbeats):
            continue
        if b_idx < 0 or b_idx + int(b_len) + 2 >= len(b.downbeats):
            continue
        if ta.downbeats[a_idx - a_ramp] < earliest - 0.05:
            continue
        if b.downbeats[b_idx] + b_len * bar_b > b.duration - B_TAIL_SAFETY:
            continue
        if not (grid_regular(ta.downbeats, a_idx - a_ramp, a_idx + bars) and b_grid_ok(b, b_idx, b_len)):
            continue  # compases irregulares en alguno de los dos tramos: ese punto no sirve
        phrase_pen = 0.0 if (a_idx - ta.phrase_offset) % 8 == 0 else 1.5
        cost = abs(ta.downbeats[a_idx] - ideal) / bar_a + phrase_pen + 40.0 * err
        if best is None or cost < best[0]:
            best = (cost, (wa, wb, a_idx, b_idx, ka, err))
    if best is None:
        return None
    wa, wb, a_idx, b_idx, ka, err = best[1]
    tw = ka * bar_t
    lanes = strategy._build_aligned(bars, bar_t, tw, wa.end - wa.start)  # type: ignore[attr-defined]
    duration = estimate_duration(ta.eff_bpm, tp.tempo, b.eff_bpm * tp.mult, a_ramp, bars, b_ramp)  # type: ignore[operator]
    out_start = ta.downbeats[a_idx - a_ramp]
    b_resume = b.downbeats[min(len(b.downbeats) - 1, b_idx + int(round(b_len)))]
    note = tr("enlaza en «{word}» dentro de una mezcla por EQ de {bars} compases", word=wa.word, bars=bars)
    if abs(tp.dev_a) >= adapt.RAMP_MIN_DEV or abs(tp.dev_b) >= adapt.RAMP_MIN_DEV:
        note += tr("; tempo común {bpm} BPM", bpm=f"{tp.tempo:.1f}")
    if kp.semitones:
        note += tr("; entrante {n} semitonos", n=f"{kp.semitones:+d}")
    render: dict[str, Any] = {
        "a_id": ta.id, "b_id": b.id, "tempo": round(tp.tempo, 4), "mult": tp.mult, "a_ramp": a_ramp,
        "bars": bars, "b_ramp": b_ramp, "a_idx": a_idx, "b_idx": b_idx, "pitch_b": kp.semitones,
        "dev_a": round(tp.dev_a, 4), "dev_b": round(tp.dev_b, 4), "stems_src": None,
        "word": wa.word, "word_error_ms": round(err * 1000, 1),
    }
    return MixPlan(strategy.name, out_start, b_resume, duration, 1.0, bars, lanes, note, tw, False, render)


def why_not(a: Track, b: Track, ctx: "RenderCtx | None" = None) -> str:
    """Por qué esta pareja no admite una mezcla a compás (para el registro y las notas del plan)."""
    if ctx is not None and not ctx.render:
        return tr("no hay worker con Rubber Band disponible (mezcla clásica: ±6 % de tempo)")
    if not (a.eff_bpm and b.eff_bpm):
        return tr("falta el tempo de una de las pistas")
    return explain(a, b)
