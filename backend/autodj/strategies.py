"""Estrategias de mezcla.

Cada estrategia dice si es viable para un par de pistas y, si lo es, genera un plan de transición con
puntos de entrada/salida y curvas de automatización (volumen y EQ).

Convenciones del plan:
- `out_start`: segundo (en el audio de A tal y como suena) en que empieza la transición.
- `in_start`: segundo del audio de B (ya estirado a `rate_b`) en que entra B.
- Los puntos de automatización son `[t, valor]` con t relativo al inicio de la transición.
  `gain` es lineal 0..1; `low`, `mid`, `high` son ganancias de EQ en dB.
- `swap_at`: instante (relativo al inicio) en que los graves cambian de una pista a la otra.
"""

from __future__ import annotations

import math
import random
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from . import adapt, camelot
from .i18n import tr
from .models import Track, TrackView, Word

KILL_DB = -40.0
MAX_STRETCH = 0.06  # ±6 % de tempo como máximo en mezclas solapadas
SWAP_RAMP = 0.04  # rampa corta para que los cambios de EQ no hagan "click"
FADE_SECONDS = 6.0  # duración del fundido de respaldo

# Preferencia por tamaño de frase al elegir dónde empezar: cuántos compases de desvío
# respecto al punto ideal se aceptan a cambio de caer en una frontera de ese nivel.
GRID_PENALTY = {32: 0.0, 16: 1.0, 8: 2.5, 4: 6.0, 1: 14.0}


@dataclass
class Lane:
    deck: str  # "A" (saliente) · "B" (entrante)
    param: str  # gain · low · mid · high
    points: list[list[float]]


@dataclass
class MixPlan:
    strategy: str
    out_start: float
    in_start: float
    duration: float
    rate_b: float
    bars: int = 0
    automation: list[Lane] = field(default_factory=list)
    notes: str = ""
    swap_at: float | None = None  # intercambio de graves, relativo al inicio de la transición
    aligned: bool = False  # el intercambio coincide con el final de la intro de B
    # Mezcla adaptativa: el worker la renderiza en un clip (ver adaptive.py y render.py). Con ella,
    # `out_start` es el instante de A (a su tempo) donde empieza el clip, `in_start` el de B donde
    # continúa al acabar, `duration` la del clip y las curvas son relativas al inicio del solapamiento.
    render: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["out_start"] = round(self.out_start, 4)
        d["in_start"] = round(self.in_start, 4)
        d["duration"] = round(self.duration, 4)
        if self.swap_at is not None:
            d["swap_at"] = round(self.swap_at, 4)
        return d


@dataclass
class StemWindow:
    """Tramo de una pista del que ya hay stems separados (ver stems.py)."""

    dir: str
    window: str  # head | tail | cut
    start: float
    end: float
    vocal: list[float] | None = None  # actividad de la voz (0–1) cada `step` s desde `start`
    step: float = 0.5


def a_stem_window(ctx: "RenderCtx | None", tid: str) -> "StemWindow | None":
    """Ventana de stems de la saliente: la del punto de corte si se recorta, y si no la del final."""
    if ctx is None or ctx.stems is None:
        return None
    return (ctx.stems(tid, "cut") if ctx.shorten else None) or ctx.stems(tid, "tail")


@dataclass
class RenderCtx:
    """Lo que el planificador necesita saber del entorno para mezclar de forma adaptativa."""

    render: bool = False  # hay un worker que renderiza clips
    shorten: bool = False  # no dejar las canciones enteras: mezclar tras el primer estribillo (ver cut_point)
    stems: Callable[[str, str], StemWindow | None] | None = None  # (id de pista, "head"|"tail") → ventana lista


def stretch_rate(bpm_a: float, bpm_b: float) -> float:
    """Velocidad a la que debe sonar B para igualar el tempo efectivo de A."""
    return round(bpm_a / bpm_b, 4)


def tempo_delta(bpm_a: float, bpm_b: float) -> float:
    """Diferencia relativa de tempo, probando también doble y mitad."""
    return min(abs(bpm_b * m - bpm_a) / bpm_a for m in (0.5, 1.0, 2.0))


def _nearest_index(values: list[float], t: float) -> int:
    return min(range(len(values)), key=lambda i: abs(values[i] - t))


def _swap_target(a: TrackView) -> float:
    """Instante de A en que su graves se van de forma natural: el inicio del outro."""
    t = a.outro_start
    if t is not None:
        return t
    outro = [s for s in a.segments() if s.label == "outro"]
    return outro[0].start if outro else a.duration * 0.78


def _target_exit(a: TrackView, urgent: bool, earliest: float) -> float:
    """Momento ideal para soltar A en las estrategias que no solapan: el outro o el último cuarto."""
    if urgent:
        return earliest
    t = a.outro_start
    if t is None:
        outro = [s for s in a.segments() if s.label == "outro"]
        t = outro[0].start if outro else a.duration * 0.72
    return max(t, earliest)


MIN_PLAY = 60.0  # s mínimos que suena una canción antes de que se la pueda recortar
MIN_TAIL = 45.0  # s que deben quedar tras el punto de corte para que merezca la pena recortar


def cut_point(a: TrackView, after: float = 0.0, min_play: float = MIN_PLAY) -> float | None:
    """Dónde recortar `a`: el final de su primer estribillo, que es donde empieza el verso siguiente.

    «Estribillo» es un tramo de energía alta del análisis (etiqueta `drop`). Se toma el primero que acabe
    después de `min_play` y de `after`, y que deje al menos `MIN_TAIL` s de canción por recortar. Si el
    análisis no encuentra ninguno, se usa un inicio de frase hacia el 55 % de la duración.
    """
    segs = sorted(a.segments(), key=lambda x: x.start)
    runs: list[list[float]] = []
    for sg in segs:
        if sg.label != "drop":
            continue
        if runs and abs(runs[-1][1] - sg.start) < 0.5:
            runs[-1][1] = sg.end
        else:
            runs.append([sg.start, sg.end])
    limit = a.duration - MIN_TAIL
    for _, end in runs:
        if min_play <= end <= limit and end >= after:
            return end
    if not runs and a.duration >= 2 * min_play:
        grid = a.grid(8) or a.grid(16) or a.downbeats
        want = max(min_play, 0.55 * a.duration, after)
        cands = [t for t in grid if want <= t <= limit]
        if cands:
            return min(cands, key=lambda t: abs(t - want))
    return None


def mix_ideal(a: TrackView, to_swap: float, ctx: "RenderCtx | None", earliest: float, lead: float = 0.0) -> float:
    """Instante ideal para que empiece el solapamiento de `a`.

    Con `ctx.shorten` es el corte tras el primer estribillo (más `lead`, la rampa de tempo que va antes);
    si no hay corte posible, o no se recorta, el de siempre: que el cambio de graves caiga en el outro.
    """
    if ctx is not None and ctx.shorten:
        cut = cut_point(a, after=earliest + lead)
        if cut is not None:
            return cut + lead
    return _swap_target(a) - to_swap


def choose_out_start(a: TrackView, earliest: float, length: float, ideal: float | None = None,
                     urgent: bool = False, skip: "set[float] | None" = None) -> float | None:
    """Elige un inicio de frase de A donde empezar la transición.

    Mira las fronteras de la rejilla de frases de A (32, 16, 8, 4 compases y, como último
    recurso, cualquier compás) entre `earliest` y el último punto que deja sitio a `length`.
    Elige la más cercana a `ideal`, pero una frontera de nivel alto vale por varios compases
    de desvío (ver GRID_PENALTY): empezar en un bloque de 32 es mejor que en uno de 8.
    """
    latest = a.duration - length - 0.25
    if latest < earliest:
        return None
    target = earliest if (urgent or ideal is None) else max(ideal, earliest)
    bar = a.bar
    best: tuple[float, float] | None = None
    for grid, penalty in GRID_PENALTY.items():
        for t in a.grid(grid):
            if t < earliest - 1e-6:
                continue
            if t > latest:
                break
            if skip and t in skip:
                continue
            cost = abs(t - target) / bar + penalty
            if best is None or cost < best[0] - 1e-9:
                best = (cost, t)
    return best[1] if best else None


def _norm_word(w: str) -> str:
    w = unicodedata.normalize("NFKD", w.lower())
    w = "".join(c for c in w if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9ñ]", "", w)


_STOP = set(
    "the and you for are but not was all can que con por para una uno los las del mas pero como "
    "esta este eso esa sin yo tu mi me te se lo la le de el en es un y o a ya si no oh ah eh yeah".split()
)


def good_words(words: list[Word], min_conf: float = 0.6) -> list[Word]:
    out = []
    for w in words:
        n = _norm_word(w.word)
        if len(n) >= 3 and n not in _STOP and w.conf >= min_conf:
            out.append(Word(n, w.start, w.end, w.conf))
    return out


def _lane(deck: str, param: str, *points: tuple[float, float]) -> Lane:
    return Lane(deck, param, [[round(t, 4), round(v, 4)] for t, v in points])


def _equal_power(length: float, rising: bool, steps: int = 4) -> list[tuple[float, float]]:
    """Curva de volumen de potencia constante: la suma de las dos pistas no se hunde a mitad."""
    pts = []
    for i in range(steps + 1):
        x = i / steps
        pts.append((x * length, math.sin(x * math.pi / 2) if rising else math.cos(x * math.pi / 2)))
    return pts


class Strategy:
    name = ""
    label = ""
    overlapping = False

    def viable(self, a: Track, b: Track, rate_a: float = 1.0,
               ctx: RenderCtx | None = None) -> bool:  # pragma: no cover - interfaz
        raise NotImplementedError

    def plan(self, a: TrackView, b: Track, earliest: float, urgent: bool = False,
             rng: random.Random | None = None,
             ctx: RenderCtx | None = None) -> MixPlan | None:  # pragma: no cover - interfaz
        raise NotImplementedError


class BarsMix(Strategy):
    """Fundido de 8 o 16 compases desde un inicio de frase, con intercambio de graves a mitad."""

    name = "bars"
    label = "Por compases"
    overlapping = True

    def _tempo_ok(self, bpm_a: float, b: Track) -> bool:
        return bool(b.eff_bpm) and abs(bpm_a / b.eff_bpm - 1) <= MAX_STRETCH  # type: ignore[operator]

    def viable(self, a: Track, b: Track, rate_a: float = 1.0, ctx: RenderCtx | None = None) -> bool:
        if not (a.eff_bpm and b.eff_bpm and b.downbeats):
            return False
        if ctx is not None and ctx.render and abs(rate_a - 1.0) < 1e-3 and a.downbeats:
            return adaptive.viable(self, a, b)
        if not self._tempo_ok(a.eff_bpm * rate_a, b):
            return False
        return camelot.harmonic_score(a.eff_camelot, b.eff_camelot, a.key_conf, b.key_conf) >= 0.6

    def _options(self, a: TrackView, b: Track, rng: random.Random, urgent: bool) -> list[int]:
        """Longitudes posibles de la mezcla en compases, de más a menos deseable."""
        h = camelot.harmonic_score(a.track.eff_camelot, b.eff_camelot, a.track.key_conf, b.key_conf)
        return self._options_h(h, rng, urgent)

    def _options_h(self, h: float, rng: random.Random, urgent: bool) -> list[int]:
        if urgent:
            return [8]
        return [16, 8] if h >= 0.85 and rng.random() < 0.6 else [8]

    def _entry(self, bv: TrackView, bars: int, length: float, aligned_only: bool) -> tuple[float, bool] | None:
        """Dónde entra B. Devuelve (instante, ¿ajustado a su intro?).

        Ajustado: B entra `bars/2` compases antes de que acabe su intro, de modo que el
        intercambio de graves (a mitad de la mezcla) cae justo cuando empieza su cuerpo.
        """
        swap = bars // 2
        db = bv.downbeats
        ie = bv.intro_end
        if ie is not None and db:
            idx = _nearest_index(db, ie)
            if idx >= swap and abs(db[idx] - ie) < bv.bar / 2:
                entry = db[idx - swap]
                if entry + length < bv.duration - 20:
                    return entry, True
        if aligned_only:
            return None
        # Sin intro aprovechable: primera frase de 8 compases desde la que cabe la mezcla.
        for t in bv.grid(8):
            if t + length < bv.duration * 0.6:
                return t, False
        return None

    def plan(self, a, b, earliest, urgent=False, rng=None, ctx=None):
        rng = rng or random.Random()
        if ctx is not None and ctx.render and abs(a.rate - 1.0) < 1e-3:
            return adaptive.plan_rendered(self, a, b, earliest, urgent, rng, ctx)
        if not self.viable(a.track, b, a.rate):
            return None
        rate_b = stretch_rate(a.bpm, b.eff_bpm)
        bv = TrackView(b, rate_b)
        bar = a.bar
        options = self._options(a, b, rng, urgent)
        if ctx is not None and ctx.shorten:
            options = [o for o in options if o <= 16] or [8]
        for aligned_only in (True, False):
            for bars in options:
                if not aligned_only and bars > 16:
                    continue  # sin una intro donde apoyarse, 32 compases solapados son demasiados
                length = bars * bar
                found = self._entry(bv, bars, length, aligned_only)
                if found is None:
                    continue
                entry, aligned = found
                out = choose_out_start(a, earliest, length, mix_ideal(a, (bars // 2) * bar, ctx, earliest), urgent)
                if out is not None:
                    return self._build(out, entry, length, rate_b, bars, bar, aligned)
        return None

    def _build(self, out, entry, length, rate_b, bars, bar, aligned) -> MixPlan:
        swap = (bars // 2) * bar
        lanes = [
            _lane("B", "gain", *_equal_power(length, True)),
            _lane("A", "gain", *_equal_power(length, False)),
            _lane("B", "low", (0, KILL_DB), (swap, KILL_DB), (swap + SWAP_RAMP, 0.0)),
            _lane("A", "low", (0, 0.0), (swap, 0.0), (swap + SWAP_RAMP, KILL_DB)),
        ]
        note = tr("fundido de {bars} compases con intercambio de graves", bars=bars)
        if aligned:
            note += tr(" al llegar la entrante a su cuerpo")
        return MixPlan(self.name, out, entry, length, rate_b, bars, lanes, note, swap, aligned)


class EqMix(BarsMix):
    """B entra sin graves; intercambio de graves en un downbeat; después se vacía A."""

    name = "eq"
    label = "EQ mixing"

    def viable(self, a: Track, b: Track, rate_a: float = 1.0, ctx: RenderCtx | None = None) -> bool:
        if not (a.eff_bpm and b.eff_bpm and b.downbeats):
            return False
        if ctx is not None and ctx.render and abs(rate_a - 1.0) < 1e-3 and a.downbeats:
            return adaptive.viable(self, a, b)
        return self._tempo_ok(a.eff_bpm * rate_a, b)

    def _options_h(self, h, rng, urgent):
        if urgent or h < 0.6:  # tonalidades sin arreglo: mezcla corta
            return [8]
        return [32, 16, 8] if h >= 0.85 else [16, 8]

    def _build(self, out, entry, length, rate_b, bars, bar, aligned) -> MixPlan:
        swap = (bars // 2) * bar
        two = 2 * bar
        lanes = [
            _lane("B", "gain", (0, 0.0), (two, 1.0)),
            _lane("B", "low", (0, KILL_DB), (swap, KILL_DB), (swap + SWAP_RAMP, 0.0)),
            _lane("B", "mid", (0, -8.0), (swap, -8.0), (length, 0.0)),
            _lane("A", "low", (0, 0.0), (swap, 0.0), (swap + SWAP_RAMP, KILL_DB)),
            _lane("A", "mid", (0, 0.0), (swap, 0.0), (length - two, -12.0)),
            _lane("A", "high", (0, 0.0), (swap, 0.0), (length - two, -18.0)),
            _lane("A", "gain", (0, 1.0), (length - two, 1.0), (length, 0.0)),
        ]
        note = tr("EQ de {bars} compases: graves cambian a mitad, luego se vacía la saliente", bars=bars)
        if aligned:
            note += tr("; el cambio coincide con el cuerpo de la entrante")
        return MixPlan(self.name, out, entry, length, rate_b, bars, lanes, note, swap, aligned)


class WordplayMix(Strategy):
    """La saliente llega a una palabra y la entrante dice esa misma palabra justo ahí.

    Con worker, el enlace va dentro de una mezcla por EQ: las dos pistas suenan a compás (la entrante sin
    graves ni brillo por debajo), el cambio de graves y de voces cae en la palabra, y después se va la
    saliente. Para que la palabra caiga a la vez en las dos, se buscan parejas cuya posición dentro del
    compás coincida. Sin worker se queda el enlace corto de antes.
    """

    name = "wordplay"
    label = "Wordplay"

    def _pairs(self, a_words: list[Word], b: Track) -> list[tuple[Word, Word]]:
        bw = [w for w in good_words(b.lyrics_words) if b.duration - w.start >= 60]
        by_word: dict[str, list[Word]] = {}
        for w in bw:
            by_word.setdefault(w.word, []).append(w)
        # (con worker se prueban todas las apariciones de la palabra en B; sin él, la primera)
        return [(w, wb) for w in good_words(a_words) for wb in by_word.get(w.word, [])]

    def viable(self, a: Track, b: Track, rate_a: float = 1.0, ctx: RenderCtx | None = None) -> bool:
        if not self._pairs(a.lyrics_words, b):
            return False
        if ctx is not None and ctx.render and abs(rate_a - 1.0) < 1e-3 and a.downbeats:
            return adaptive.viable(self, a, b)
        return True

    def plan(self, a, b, earliest, urgent=False, rng=None, ctx=None):
        rng = rng or random.Random()
        if ctx is not None and ctx.render and abs(a.rate - 1.0) < 1e-3 and a.track.downbeats:
            return adaptive.plan_wordplay(self, a, b, earliest, urgent, rng, ctx)
        by_first: dict[str, Word] = {}
        for _, wb in self._pairs(a.words(), b):
            by_first.setdefault(wb.word, wb)
        pairs = [(wa, by_first[wa.word]) for wa, _ in self._pairs(a.words(), b) if wa.start >= earliest]
        if not pairs:
            return None
        target = _target_exit(a, urgent, earliest)
        late = [p for p in pairs if p[0].start >= target]
        wa, wb = late[0] if late else pairs[-1]
        fade = 0.12
        lanes = [
            _lane("A", "gain", (0, 1.0), (fade, 0.0)),
            _lane("B", "gain", (0, 0.0), (fade, 1.0)),
        ]
        return MixPlan(self.name, wa.start, wb.start, fade, 1.0, 0, lanes,
                       tr("enlaza en la palabra «{word}»", word=wa.word))

    def _build_aligned(self, bars: int, bar: float, tw: float, word_len: float) -> list[Lane]:
        """Curvas de una mezcla por EQ con el enlace de palabra en `tw` (s desde el inicio del solapamiento)."""
        length = bars * bar
        wl = min(0.7, max(0.2, word_len))
        end_a = tw + wl
        return [
            _lane("B", "gain", (0, 0.0), (min(2 * bar, tw), 1.0)),
            _lane("B", "low", (0, KILL_DB), (tw, KILL_DB), (tw + SWAP_RAMP, 0.0)),
            _lane("B", "mid", (0, -14.0), (max(0.0, tw - 0.06), -14.0), (tw + 0.04, 0.0)),
            _lane("B", "high", (0, -10.0), (max(0.0, tw - 0.06), -10.0), (tw + 0.04, 0.0)),
            _lane("A", "low", (0, 0.0), (tw, 0.0), (tw + SWAP_RAMP, KILL_DB)),
            _lane("A", "mid", (0, 0.0), (end_a, 0.0), (end_a + 0.3, -18.0)),
            _lane("A", "high", (0, 0.0), (end_a, 0.0), (end_a + 0.3, -16.0)),
            _lane("A", "gain", (0, 1.0), (max(end_a + 0.3, length - 2 * bar), 1.0), (length, 0.0)),
        ]


class QuickMix(EqMix):
    """Mezcla corta por EQ (4 compases) para parejas cuyas tonalidades chocan.

    Los compases de rampa de tempo van antes y después y suenan en solitario; lo que se solapa es poco:
    la saliente pierde medios y agudos enseguida, el bajo cambia a mitad y la entrante recupera
    sus medios al final. No hay tiempo de que se oiga el choque.
    """

    name = "quick"
    label = "Mezcla corta"

    def viable(self, a: Track, b: Track, rate_a: float = 1.0, ctx: RenderCtx | None = None) -> bool:
        if ctx is None or not ctx.render or abs(rate_a - 1.0) > 1e-3 or not (a.downbeats and b.downbeats):
            return False
        return adaptive.viable(self, a, b)

    def _options_h(self, h, rng, urgent):
        return [4]

    def _build(self, out, entry, length, rate_b, bars, bar, aligned) -> MixPlan:
        half = (bars // 2) * bar
        lanes = [
            _lane("B", "gain", (0, 0.0), (0.5 * bar, 1.0)),
            _lane("B", "low", (0, KILL_DB), (half, KILL_DB), (half + SWAP_RAMP, 0.0)),
            _lane("B", "mid", (0, -30.0), (half, -30.0), (length - 0.5 * bar, 0.0)),
            _lane("B", "high", (0, -8.0), (half, -4.0), (length, 0.0)),
            _lane("A", "low", (0, 0.0), (half, 0.0), (half + SWAP_RAMP, KILL_DB)),
            _lane("A", "mid", (0, 0.0), (0.5 * bar, -24.0)),
            _lane("A", "high", (0, 0.0), (half, -8.0), (length, -24.0)),
            _lane("A", "gain", (0, 1.0), (length - bar, 1.0), (length, 0.0)),
        ]
        return MixPlan(self.name, out, entry, length, rate_b, bars, lanes,
                       tr("mezcla corta de {bars} compases: las tonalidades chocan, así que se solapa lo mínimo", bars=bars),
                       half, aligned)


class EchoCut(Strategy):
    """Corte sobre el compás con eco: la saliente se corta en un inicio de frase, su eco se queda sonando
    y la entrante arranca justo ahí a su propio tempo. Para parejas cuyo tempo no se puede igualar: no
    hay dos ritmos solapados, pero tampoco un hueco ni un fundido."""

    name = "cut"
    label = "Corte con eco"

    def viable(self, a: Track, b: Track, rate_a: float = 1.0, ctx: RenderCtx | None = None) -> bool:
        return bool(ctx is not None and ctx.render and abs(rate_a - 1.0) < 1e-3
                    and len(a.downbeats) >= 16 and len(b.downbeats) >= 16)

    def plan(self, a, b, earliest, urgent=False, rng=None, ctx=None):
        if not self.viable(a.track, b, a.rate, ctx):
            return None
        return adaptive.plan_cut(self, a, b, earliest, urgent, rng or random.Random(), ctx)


class StemsMix(BarsMix):
    """Mezcla con stems: el bajo cambia de una pista a otra sin tocar el bombo, las voces no se pisan
    y la armonía de la entrante solo llega cuando se va la de la saliente. Solo si los stems de
    las dos pistas ya están separados."""

    name = "stems"
    label = "Stems"

    def viable(self, a: Track, b: Track, rate_a: float = 1.0, ctx: RenderCtx | None = None) -> bool:
        if ctx is None or not ctx.render or ctx.stems is None or abs(rate_a - 1.0) > 1e-3:
            return False
        if not (a.eff_bpm and b.eff_bpm and a.downbeats and b.downbeats):
            return False
        if a_stem_window(ctx, a.id) is None or ctx.stems(b.id, "head") is None:
            return False
        return adaptive.viable(self, a, b)

    def _options_h(self, h, rng, urgent):
        if urgent:
            return [8]
        return [16, 8] if h < 0.85 else [32, 16, 8]

    def plan(self, a, b, earliest, urgent=False, rng=None, ctx=None):
        rng = rng or random.Random()
        if not self.viable(a.track, b, a.rate, ctx):
            return None
        return adaptive.plan_rendered(self, a, b, earliest, urgent, rng, ctx)

    def _build(self, out, entry, length, rate_b, bars, bar, aligned, harmony: float = 1.0) -> MixPlan:
        swap = (bars // 2) * bar
        two = 2 * bar
        total = bars * bar
        compatible = harmony >= 0.85
        b_other_in = swap - (4 * bar if compatible else 0.0)
        a_other_out = swap + (two if compatible else 0.0)
        lanes = [
            # Entrante: primero el ritmo, el bajo en el cambio, la armonía después y la voz la última.
            _lane("B", "drums", (0, 0.0), (two, 1.0)),
            _lane("B", "bass", (0, 0.0), (swap, 0.0), (swap + SWAP_RAMP, 1.0)),
            _lane("B", "other", (0, 0.0), (max(0.0, b_other_in), 0.0), (swap + two, 1.0)),
            _lane("B", "vocals", (0, 0.0), (swap + two, 0.0), (swap + two + bar, 1.0)),
            # Saliente: la voz se va pronto para que no se pisen, el bajo cambia, el ritmo aguanta.
            _lane("A", "vocals", (0, 1.0), (total * 0.25, 1.0), (total * 0.25 + two, 0.0)),
            _lane("A", "other", (0, 1.0), (max(0.0, swap - two), 1.0), (a_other_out, 0.0)),
            _lane("A", "bass", (0, 1.0), (swap, 1.0), (swap + SWAP_RAMP, 0.0)),
            _lane("A", "drums", (0, 1.0), (total - 4 * bar, 1.0), (total - bar, 0.0)),
        ]
        note = tr("stems de {bars} compases: bajo y armonía cambian a mitad, las voces no se pisan", bars=bars)
        if aligned:
            note += tr("; el cambio coincide con el cuerpo de la entrante")
        return MixPlan(self.name, out, entry, length, rate_b, bars, lanes, note, swap, aligned)


class RelayMix(StemsMix):
    """Relevo de voz: la entrante llega solo con su base (batería, bajo y armonía) y la saliente se queda
    cantando encima; cuando la saliente acaba su frase, entra la voz de la entrante.

    Hace falta que las tonalidades encajen (la voz de una suena sobre la base de la otra), que la saliente
    cante justo al empezar la mezcla y que la entrante no cante antes de que acabe la otra. Eso se sabe por la
    actividad de la voz que guarda el worker junto a los stems (ver adaptive.relay_points).
    """

    name = "relay"
    label = "Relevo de voz"
    relay = True

    def _options_h(self, h, rng, urgent):
        return [8] if urgent else [16, 8]

    def _build(self, out, entry, length, rate_b, bars, bar, aligned, harmony: float = 1.0,
               v_end: float | None = None, v_in: float | None = None) -> MixPlan:
        total = bars * bar
        v_end = total * 0.5 if v_end is None else v_end
        v_in = v_end if v_in is None else v_in
        lanes = [
            # La base de la entrante llega primero: ritmo, bajo (cambia con el de la saliente) y armonía.
            _lane("B", "drums", (0, 0.0), (bar, 1.0)),
            _lane("B", "bass", (0, 0.0), (2 * bar, 0.0), (2 * bar + SWAP_RAMP, 1.0)),
            _lane("B", "other", (0, 0.0), (bar, 0.0), (3 * bar, 1.0)),
            _lane("B", "vocals", (0, 0.0), (v_in, 0.0), (v_in + 0.5 * bar, 1.0)),
            # La base de la saliente se va y su voz se queda hasta acabar la frase.
            _lane("A", "drums", (0, 1.0), (2 * bar, 1.0), (3 * bar, 0.0)),
            _lane("A", "bass", (0, 1.0), (2 * bar, 1.0), (2 * bar + SWAP_RAMP, 0.0)),
            _lane("A", "other", (0, 1.0), (2 * bar, 1.0), (4 * bar, 0.0)),
            _lane("A", "vocals", (0, 1.0), (v_end, 1.0), (min(total, v_end + 0.5 * bar), 0.0)),
        ]
        note = tr("relevo de voz de {bars} compases: la voz de la saliente sigue sobre la base de la entrante "
                  "hasta el compás {bar}", bars=bars, bar=int(round(v_end / bar)))
        return MixPlan(self.name, out, entry, length, rate_b, bars, lanes, note, 2 * bar, aligned)


STRATEGIES: dict[str, Strategy] = {s.name: s for s in (BarsMix(), EqMix(), WordplayMix(), StemsMix(), RelayMix(),
                                                         QuickMix(), EchoCut())}
# Recursos de última hora: nunca se eligen mientras haya una mezcla de verdad que encaje.
LAST_RESORT = ("quick", "cut")


def viable_strategies(a: Track, b: Track, rate_a: float = 1.0, ctx: RenderCtx | None = None) -> list[str]:
    """Estrategias que encajan para la pareja. «cut» no se lista: siempre es posible y no es una mezcla."""
    return [name for name, s in STRATEGIES.items() if name != "cut" and s.viable(a, b, rate_a, ctx)]


def plan_transition(a: TrackView, b: Track, preferred: str | None, earliest: float,
                    urgent: bool = False, rng: random.Random | None = None,
                    avoid: str | None = None, ctx: RenderCtx | None = None) -> MixPlan:
    """Plan con la estrategia preferida; si no es posible, prueba las demás.

    Orden: la preferida, las mezclas que encajan (con variedad), la mezcla corta, el corte con eco y,
    solo si nada de eso se puede (sin worker o sin compases detectados), el fundido suave.
    """
    rng = rng or random.Random()
    order = [preferred] if preferred in STRATEGIES else []
    viable = [n for n in viable_strategies(a.track, b, a.rate, ctx) if n not in order]
    rest = [n for n in viable if n not in LAST_RESORT]
    last = [n for n in viable if n in LAST_RESORT]
    rng.shuffle(rest)
    if avoid in rest and len(rest) > 1:  # variedad: no repetir la misma estrategia seguida
        rest.remove(avoid)
        rest.append(avoid)
    if not order:  # con stems listos, son las mejores opciones
        withstems = [n for n in rest if n in ("stems", "relay")]
        rest = withstems + [n for n in rest if n not in withstems]
    for name in order + rest + last + ["cut"]:
        p = STRATEGIES[name].plan(a, b, earliest, urgent, rng, ctx)
        if p is not None:
            if name == "cut" or (name in LAST_RESORT and preferred != name):
                p.notes += tr(" · motivo: {why}", why=adaptive.why_not(a.track, b))
            return p
    return fade_transition(a, b, earliest, urgent, ctx, reason=adaptive.why_not(a.track, b, ctx))


def fade_transition(a: TrackView, b: Track, earliest: float, urgent: bool = False,
                    ctx: RenderCtx | None = None, reason: str = "") -> MixPlan:
    """Respaldo cuando no cabe una mezcla a tempo: A se apaga quitando graves y B entra
    desde su intro, sin solapar dos ritmos a volumen alto. Nunca es un corte en seco."""
    length = FADE_SECONDS
    ideal = mix_ideal(a, 0.0, ctx, earliest) if ctx is not None and ctx.shorten else _target_exit(a, urgent, earliest)
    out = choose_out_start(a, earliest, length, ideal, urgent)
    if out is None:
        out = max(earliest, min(a.duration - length - 0.2, earliest))
    entry = b.downbeats[0] if b.downbeats else 0.0
    lanes = [
        _lane("A", "low", (0, 0.0), (length * 0.5, KILL_DB)),
        _lane("A", "gain", (0, 1.0), (length * 0.4, 0.6), (length, 0.0)),
        _lane("B", "gain", (0, 0.0), (length * 0.5, 0.25), (length, 1.0)),
        _lane("B", "low", (0, KILL_DB), (length * 0.6, KILL_DB), (length, 0.0)),
    ]
    why = reason or tr("el tempo no permite mezclar a compás")
    return MixPlan("fade", out, entry, length, 1.0, 0, lanes, tr("fundido suave: {why}", why=why))


from . import adaptive  # noqa: E402  (al final: adaptive usa lo definido arriba)
