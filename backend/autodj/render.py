"""Renderizado de una transición: las dos pistas, ya adaptadas, mezcladas en un único clip de audio.

El navegador reproduce la saliente hasta el inicio del clip, el clip, y sigue con la entrante donde acaba.
El estirado, el cambio de tono, los filtros y los stems se aplican aquí, en el worker.

Línea de tiempo del clip (compases de salida, a tempo variable):

    ├── rampa de A ──┼────── solapamiento ──────┼── rampa de B ──┤
    A: tempo propio → T      A y B a tempo T          A fuera
    B:                       B: bpm_b → T         B: T → tempo propio (y tono de vuelta)

Las dos pistas se alinean por una rejilla de compases ajustada por mínimos cuadrados, de modo que los
bombos coinciden.
"""

from __future__ import annotations

import logging
import math
import os
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from scipy.signal import lfilter

from . import stretch
from .i18n import tr
from .stretch import SR

log = logging.getLogger(__name__)

CTX = 0.45  # s de audio de contexto a cada lado de cada trozo que se estira
TARGET_LUFS = -11.0  # el mismo que usa el navegador para igualar volúmenes
EQ_LOW_HZ = 200.0
EQ_HIGH_HZ = 4000.0
KILL_FLOOR_DB = -40.0
STEM_NAMES = ("drums", "bass", "other", "vocals")


class RenderError(RuntimeError):
    pass


class StemsMissing(RenderError):
    pass


def loudness_gain(lufs: float | None) -> float:
    """Ganancia con la que el navegador reproduce una pista (igual que `loudnessGain` en mix-engine.ts)."""
    if lufs is None or not math.isfinite(lufs):
        return 1.0
    return float(min(2.5, max(0.25, 10 ** ((TARGET_LUFS - lufs) / 20))))


@dataclass
class StemsRef:
    dir: str
    window: str  # "head" | "tail"
    start: float  # s de la pista original donde empieza la ventana
    end: float


@dataclass
class DeckSpec:
    id: str
    path: str
    bpm: float
    duration: float
    downbeats: list[float]
    trim: float = 1.0
    stems: StemsRef | None = None


@dataclass
class RenderSpec:
    a: DeckSpec
    b: DeckSpec
    tempo: float  # tempo común durante el solapamiento (BPM de compás de salida)
    mult: float  # compases de salida por compás de B (1, 2 o 0.5)
    a_ramp: int  # compases de rampa de A hacia el tempo común (0 = ninguna)
    bars: int  # compases de solapamiento
    b_ramp: int  # compases de rampa de B de vuelta a su tempo
    a_idx: int  # índice del downbeat de A donde empieza el solapamiento
    b_idx: int  # índice del downbeat de B donde entra
    pitch_b: int = 0  # semitonos que sube/baja B durante la mezcla
    lanes: list[dict[str, Any]] = field(default_factory=list)  # {"deck","param","points"} relativas al solapamiento
    out_name: str = "clip.wav"
    kind: str = "mix"  # "mix" (solapamiento a tempo común) · "echo_cut" (corte con eco, sin estirar)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RenderSpec":
        def deck(x: dict[str, Any]) -> DeckSpec:
            x = dict(x)
            s = x.pop("stems", None)
            return DeckSpec(**x, stems=StemsRef(**s) if s else None)

        d = dict(d)
        a, b = deck(d.pop("a")), deck(d.pop("b"))
        return RenderSpec(a=a, b=b, **d)


@dataclass(frozen=True)
class Grid:
    t0: float
    length: float  # s por compás
    rms: float  # s de error medio de los downbeats respecto a la recta
    kept: float = 1.0  # fracción de downbeats que encajan con la recta (el resto se descartó como valor atípico)

    @property
    def ok(self) -> bool:
        return self.rms <= GRID_MAX_RMS and self.kept >= GRID_MIN_KEPT

    def time(self, idx: float) -> float:
        return self.t0 + self.length * idx


GRID_MAX_RMS = 0.045  # s: por encima, la rejilla de compases es irregular y no se mezcla a compás
GRID_MIN_KEPT = 0.75  # y si hay que tirar más de una cuarta parte de los compases para que salga una recta, tampoco


def grid_regular(downbeats: list[float], lo: int, hi: int) -> bool:
    """¿Son los compases de ese tramo lo bastante regulares para alinear dos pistas? (El mismo criterio
    con el que el render acepta o rechaza el trabajo, para descartar el tramo antes de pedirlo.)"""
    try:
        return fit_grid(downbeats, lo, hi).ok
    except RenderError:
        return False


def fit_grid(downbeats: list[float], lo: int, hi: int, pad: int = 6) -> Grid:
    """Recta tiempo = t0 + L·índice ajustada a los downbeats entre `lo` y `hi` (con holgura)."""
    n = len(downbeats)
    if n < 4:
        raise RenderError(tr("la pista tiene muy pocos compases detectados"))
    i0, i1 = max(0, lo - pad), min(n - 1, hi + pad)
    if i1 - i0 < 3:
        i0, i1 = 0, n - 1
    idx = np.arange(i0, i1 + 1, dtype=float)
    t = np.asarray(downbeats[i0:i1 + 1], dtype=float)
    keep = np.ones(len(idx), bool)
    slope, icpt = 0.0, 0.0
    for _ in range(3):
        slope, icpt = np.polyfit(idx[keep], t[keep], 1)
        r = t - (icpt + slope * idx)
        mad = float(np.median(np.abs(r[keep] - np.median(r[keep])))) * 1.4826
        new = np.abs(r) <= max(0.03, 3.5 * mad)
        if new.sum() < 4 or (new == keep).all():
            break
        keep = new
    r = t[keep] - (icpt + slope * idx[keep])
    return Grid(float(icpt), float(slope), float(np.sqrt(np.mean(r ** 2))), float(keep.mean()))


@dataclass(frozen=True)
class Unit:
    phase: str  # a_ramp | overlap | b_ramp
    bars: float  # compases de salida que dura
    tempo: float  # BPM de salida durante la unidad
    k: int = 0  # índice dentro de su fase
    n: int = 1  # unidades que tiene su fase

    @property
    def seconds(self) -> float:
        return self.bars * 240.0 / self.tempo


def build_units(bpm_a: float, tempo: float, b_eff: float, a_ramp: int, bars: int, b_ramp: int) -> list[Unit]:
    units: list[Unit] = []
    n = 2 * a_ramp
    for k in range(n):
        units.append(Unit("a_ramp", 0.5, bpm_a + (tempo - bpm_a) * (k + 0.5) / n, k, n))
    units.append(Unit("overlap", float(bars), tempo))
    n = 2 * b_ramp
    for k in range(n):
        units.append(Unit("b_ramp", 0.5, tempo + (b_eff - tempo) * (k + 0.5) / n, k, n))
    return units


def estimate_duration(bpm_a: float, tempo: float, b_eff: float, a_ramp: int, bars: int, b_ramp: int) -> float:
    return sum(u.seconds for u in build_units(bpm_a, tempo, b_eff, a_ramp, bars, b_ramp))


class Source:
    """Un tramo de audio decodificado (n, 2) que empieza en `start` s de la pista original."""

    def __init__(self, data: np.ndarray, start: float):
        self.data = data
        self.start = start

    def get(self, i0: int, n: int) -> np.ndarray:
        """`n` muestras desde la muestra absoluta `i0` (de la pista original); silencio fuera del tramo."""
        base = int(round(self.start * SR))
        out = np.zeros((n, 2), np.float32)
        lo, hi = max(i0, base), min(i0 + n, base + len(self.data))
        if hi > lo:
            out[lo - i0:hi - i0] = self.data[lo - base:hi - base]
        return out


def _need(src0: float, src1: float) -> tuple[float, float]:
    return max(0.0, src0 - CTX - 0.2), src1 + CTX + 0.2


def _load_mix(deck: DeckSpec, lo: float, hi: float) -> dict[str, Source]:
    data = stretch.decode(deck.path, lo, hi - lo)
    return {"mix": Source(data, lo)}


def _load_stems(deck: DeckSpec, lo: float, hi: float, need: tuple[float, float]) -> dict[str, Source]:
    """Stems del tramo [lo, hi]. `need` es lo imprescindible: el contexto de alrededor puede quedar fuera de la ventana."""
    s = deck.stems
    assert s is not None
    if need[0] < s.start - 1e-3 or need[1] > s.end + 1e-3:
        raise StemsMissing(f"los stems de {deck.id} no cubren {need[0]:.1f}–{need[1]:.1f} s (tienen {s.start:.1f}–{s.end:.1f})")
    lo, hi = max(lo, s.start), min(hi, s.end)
    out: dict[str, Source] = {}
    for name in STEM_NAMES:
        p = Path(s.dir) / f"{s.window}_{name}.flac"
        if not p.exists():
            raise StemsMissing(f"falta {p}")
        out[name] = Source(stretch.decode(str(p), lo - s.start, hi - lo), lo)
    return out


def lane_points(lanes: list[dict[str, Any]], deck: str, param: str) -> list[list[float]] | None:
    for lane in lanes:
        if lane["deck"] == deck and lane["param"] == param and lane["points"]:
            return lane["points"]
    return None


def envelope(points: list[list[float]] | None, t: np.ndarray, default: float, db: bool = False) -> np.ndarray:
    """Valor de la curva en cada instante `t` (relativo al solapamiento); constante fuera de los extremos."""
    if not points:
        return np.full(len(t), default, np.float32)
    xs = np.array([p[0] for p in points], float)
    ys = np.array([p[1] for p in points], float)
    v = np.interp(t, xs, ys)
    if db:
        v = np.where(v <= KILL_FLOOR_DB + 1e-6, 0.0, 10 ** (v / 20))
    return v.astype(np.float32)


def _eq_bands(x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Graves / medios / agudos de fase cero que suman exactamente la señal original."""
    from scipy.signal import butter, sosfiltfilt

    lo = sosfiltfilt(butter(2, EQ_LOW_HZ, "lowpass", fs=SR, output="sos"), x, axis=0)
    hi = sosfiltfilt(butter(2, EQ_HIGH_HZ, "highpass", fs=SR, output="sos"), x, axis=0)
    return lo.astype(np.float32), (x - lo - hi).astype(np.float32), hi.astype(np.float32)


def apply_eq(x: np.ndarray, t: np.ndarray, lanes: list[dict[str, Any]], deck: str) -> np.ndarray:
    pts = {p: lane_points(lanes, deck, p) for p in ("low", "mid", "high")}
    if not any(pts.values()):
        return x
    lo, mid, hi = _eq_bands(x)
    g = {p: envelope(pts[p], t, 0.0, db=True) if pts[p] else np.ones(len(t), np.float32) for p in pts}
    return lo * g["low"][:, None] + mid * g["mid"][:, None] + hi * g["high"][:, None]


def soft_limit(y: np.ndarray, knee: float = 0.9) -> np.ndarray:
    a = np.abs(y)
    over = a > knee
    if not over.any():
        return y
    out = y.copy()
    out[over] = np.sign(y[over]) * (knee + (1 - knee) * np.tanh((a[over] - knee) / (1 - knee)))
    return out


@dataclass
class _DeckUnit:
    ui: int  # índice de la unidad del reloj de salida
    src0: float  # s de la pista original donde empieza el tramo
    src1: float
    pitch: float  # factor de frecuencia


@dataclass
class _Plan:
    """Calendario de estirado de una pista: el mismo para todos sus componentes."""

    seg_start: int  # muestra absoluta de la pista donde empieza el audio que se estira
    seg_len: int
    pre_n: int
    facs: list[float]  # factor de tempo de cada unidad
    schedule: list[tuple[float, float, float]]
    base: int  # muestra de la salida donde empieza la primera unidad


def _plan_deck(dunits: list[_DeckUnit], edges: list[int]) -> _Plan:
    pre_n = int(round(CTX * SR))
    first, last = dunits[0], dunits[-1]
    a_first, a_last = int(round(first.src0 * SR)), int(round(last.src1 * SR))
    seg_start = a_first - pre_n
    facs = []
    for u in dunits:
        n_src = int(round(u.src1 * SR)) - int(round(u.src0 * SR))
        facs.append(n_src / max(1, edges[u.ui + 1] - edges[u.ui]))
    schedule = [(0.0, facs[0], first.pitch)]
    for u, x in zip(dunits[1:], facs[1:]):
        schedule.append(((int(round(u.src0 * SR)) - seg_start) / SR, x, u.pitch))
    return _Plan(seg_start, (a_last - a_first) + 2 * pre_n, pre_n, facs, schedule, int(round(pre_n / facs[0])))


def _click_train(n: int, step: float = 0.125) -> tuple[np.ndarray, np.ndarray, list[np.ndarray]]:
    """Tren de clics de ruido (formas distintas) con el que se mide el desfase del estirador."""
    rng = np.random.default_rng(11)
    x = np.zeros((n, 2), np.float32)
    pos = np.arange(int(0.3 * SR), n - int(0.3 * SR), int(step * SR))
    shapes = [(rng.standard_normal(300) * np.exp(-np.arange(300) / 50)).astype(np.float32) for _ in pos]
    for i, b in zip(pos, shapes):
        x[i:i + 300, 0] += b
        x[i:i + 300, 1] += b
    return x, pos, shapes


def _env(sig: np.ndarray) -> np.ndarray:
    e = np.abs(np.diff(sig, prepend=0.0))
    k = np.ones(44, np.float32) / 44
    return np.convolve(e, k, mode="same")


def measure_lags(plan: _Plan, dunits: list[_DeckUnit], edges: list[int]) -> list[int]:
    """Desfase (muestras; positivo = sale tarde) del estirador en cada unidad, con este mismo calendario."""
    from scipy.signal import fftconvolve

    x, pos, shapes = _click_train(plan.seg_len)
    y = stretch.stretch_scheduled(x, plan.schedule)
    ey = _env(y[:, 0])
    first_edge = edges[dunits[0].ui]
    half = int(0.04 * SR)
    lags: list[int] = []
    for j, u in enumerate(dunits):
        # tramo de origen de la unidad, relativo al inicio del audio estirado
        s0 = int(round(u.src0 * SR)) - plan.seg_start
        s1 = int(round(u.src1 * SR)) - plan.seg_start
        out0 = plan.base + (edges[u.ui] - first_edge)
        found = []
        for i, b in zip(pos, shapes):
            if not (s0 + int(0.03 * SR) <= i <= s1 - int(0.05 * SR)):
                continue
            pred = out0 + int(round((i - s0) / plan.facs[j]))
            lo, hi = max(0, pred - half), min(len(ey), pred + half + 300)
            if hi - lo < 2 * half:
                continue
            ref = np.zeros(hi - lo, np.float32)
            ref[pred - lo:pred - lo + 300] = b
            c = fftconvolve(ey[lo:hi], _env(ref)[::-1], mode="full")
            found.append(int(np.argmax(c)) - (hi - lo - 1))
        if found:
            lags.append(int(round(float(np.median(found)))))
        else:
            lags.append(lags[-1] if lags else 0)
    return lags


def lag_curve(dunits: list[_DeckUnit], edges: list[int], lags: list[int]) -> tuple[np.ndarray, np.ndarray]:
    """Nudos (muestra de salida, desfase) para interpolar el desfase de forma continua.

    Las unidades cortas (rampas) aportan un nudo en su centro; las largas (el solapamiento) uno
    al principio y otro al final, porque su desfase es estable y no debe arrastrarse desde la rampa.
    """
    xs: list[float] = []
    ys: list[float] = []
    for u, lag in zip(dunits, lags):
        a, b = edges[u.ui], edges[u.ui + 1]
        if b - a <= 3 * SR:
            xs.append((a + b) / 2)
            ys.append(float(lag))
        else:
            xs += [a + 0.5 * SR, b - 0.5 * SR]
            ys += [float(lag), float(lag)]
    return np.array(xs), np.array(ys)


def _cubic_take(y: np.ndarray, pos: np.ndarray) -> np.ndarray:
    """`y` evaluada en posiciones fraccionarias (interpolación cúbica de Catmull-Rom)."""
    n = len(y)
    i = np.floor(pos).astype(np.int64)
    f = (pos - i).astype(np.float32)[:, None]

    def tap(k: int) -> np.ndarray:
        idx = i + k
        out = y[np.clip(idx, 0, n - 1)]
        out[(idx < 0) | (idx >= n)] = 0.0
        return out

    p0, p1, p2, p3 = tap(-1), tap(0), tap(1), tap(2)
    return p1 + 0.5 * f * (p2 - p0 + f * (2 * p0 - 5 * p1 + 4 * p2 - p3 + f * (3 * (p1 - p2) + p3 - p0)))


def _render_component(src: Source, dunits: list[_DeckUnit], edges: list[int], total: int,
                      plan: _Plan, lags: list[int]) -> np.ndarray:
    """Un componente (la mezcla completa o un stem) de una pista, estirado de una sola pasada.

    Todas las unidades de la pista pasan por un único proceso de Rubber Band con un calendario
    de tempo y tono, así que no hay uniones. Devuelve (total, 2) con el audio colocado en el reloj
    de salida y silencio donde la pista no suena. El desfase medido se compensa con un retardo
    fraccionario que varía de forma continua, para no introducir saltos en notas sostenidas.
    """
    seg = src.get(plan.seg_start, plan.seg_len)
    y = stretch.stretch_scheduled(seg, plan.schedule)
    first_edge = edges[dunits[0].ui]
    last_edge = edges[dunits[-1].ui + 1]
    kx, ky = lag_curve(dunits, edges, lags)

    out = np.zeros((total, 2), np.float32)
    chunk = 1 << 20
    for lo in range(first_edge, last_edge, chunk):
        hi = min(last_edge, lo + chunk)
        m = np.arange(lo, hi, dtype=np.float64)
        pos = plan.base + (m - first_edge) + np.interp(m, kx, ky)
        out[lo:hi] = _cubic_take(y, pos)
    return out


def _write_wav(clip: np.ndarray, final: Path) -> None:
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_suffix(".tmp")
    pcm = (np.clip(clip, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    os.replace(tmp, final)


ECHO_TAPS = 6
ECHO_FEEDBACK = 0.6
ECHO_HANDOFF = 0.005  # s de fundido en los empalmes del corte con eco


def _render_echo_cut(spec: RenderSpec, out_dir: Path) -> dict[str, Any]:
    """Corte con eco: A se corta en el downbeat `a_idx`, el último pulso de A se repite con eco (retardo de
    corchea con puntillo, cada repetición más suave y más oscura) y B arranca a su tempo desde `b_idx`.
    No se estira nada: sirve cuando los tempos o las tonalidades no se pueden igualar."""
    t_start = time.time()
    if not (0 <= spec.a_idx < len(spec.a.downbeats) and 0 <= spec.b_idx < len(spec.b.downbeats)):
        raise RenderError(tr("el corte cae fuera de la rejilla"))
    t_a = spec.a.downbeats[spec.a_idx]
    t_b = spec.b.downbeats[spec.b_idx]
    beat_a = 60.0 / spec.a.bpm
    beat_b = 60.0 / spec.b.bpm
    total = int(round(spec.bars * 4 * beat_b * SR))
    clip = np.zeros((total, 2), np.float32)

    # Entrante: tal cual, con un fundido mínimo para no hacer click en el empalme.
    b = stretch.decode(spec.b.path, t_b, total / SR + 0.05)[:total]
    n = min(len(b), total)
    h = max(1, int(ECHO_HANDOFF * SR))
    b = b[:n] * np.float32(spec.b.trim)
    b[:h] *= np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    clip[:n] += b

    # Eco de la saliente: el último pulso antes del corte, repetido y apagándose.
    seg_len = beat_a
    a = stretch.decode(spec.a.path, max(0.0, t_a - seg_len), min(seg_len, t_a))
    a = a * np.float32(spec.a.trim)
    if len(a):
        hh = min(h, len(a))
        a[-hh:] *= np.linspace(1.0, 0.0, hh, dtype=np.float32)[:, None]
        delay = int(round(0.75 * beat_a * SR))
        gain = 1.0
        lp = 0.0  # cada repetición pierde brillo
        for k in range(1, ECHO_TAPS + 1):
            gain *= ECHO_FEEDBACK
            lp = min(0.85, lp + 0.15)
            rep = a.copy()
            if lp:  # paso bajo de un polo, más cerrado en cada repetición
                rep = lfilter([1.0 - lp], [1.0, -lp], rep, axis=0).astype(np.float32)
            o = k * delay
            if o >= total:
                break
            m = min(len(rep), total - o)
            clip[o:o + m] += rep[:m] * np.float32(gain)
    clip = soft_limit(clip)
    _write_wav(clip, out_dir / spec.out_name)
    return {
        "file": spec.out_name, "duration": round(total / SR, 4), "kind": "echo_cut", "handoff": ECHO_HANDOFF,
        "a_start": round(t_a, 4), "b_resume": round(t_b + total / SR, 4),
        "overlap_at": 0.0, "overlap_end": 0.0, "maps": {"a": [], "b": []}, "stems": False,
        "engine": "native", "grid_rms_ms": {"a": 0.0, "b": 0.0}, "render_s": round(time.time() - t_start, 2),
    }


def render_transition(spec: RenderSpec, out_dir: Path, progress: Any = None) -> dict[str, Any]:
    if spec.kind == "echo_cut":
        return _render_echo_cut(spec, out_dir)
    t_start = time.time()
    bpm_a, bpm_b = spec.a.bpm, spec.b.bpm
    b_eff = bpm_b * spec.mult
    units = build_units(bpm_a, spec.tempo, b_eff, spec.a_ramp, spec.bars, spec.b_ramp)

    # Reloj de salida: muestra donde empieza cada unidad (acumulado exacto, sin deriva).
    bounds = [0.0]
    for u in units:
        bounds.append(bounds[-1] + u.seconds)
    edges = [int(round(t * SR)) for t in bounds]
    total = edges[-1]
    ov = 2 * spec.a_ramp  # índice de la unidad de solapamiento
    t_ov0, t_ov1 = bounds[ov], bounds[ov + 1]

    # Rejillas ajustadas
    ia0 = spec.a_idx - spec.a_ramp
    if ia0 < 0:
        raise RenderError(tr("no hay compases suficientes antes del solapamiento en A"))
    b_len_bars = (spec.bars + spec.b_ramp) / spec.mult
    ga = fit_grid(spec.a.downbeats, ia0, spec.a_idx + spec.bars)
    gb = fit_grid(spec.b.downbeats, spec.b_idx, int(math.ceil(spec.b_idx + b_len_bars)))
    if not (ga.ok and gb.ok):
        raise RenderError(tr("rejilla de compases irregular (A {a_ms} ms / {a_ok} válidos, B {b_ms} ms / {b_ok} válidos)",
                             a_ms=f"{ga.rms * 1000:.0f}", a_ok=f"{ga.kept:.0%}",
                             b_ms=f"{gb.rms * 1000:.0f}", b_ok=f"{gb.kept:.0%}"))

    # Tramo de cada pista en cada unidad
    out_bar_start: list[float] = []
    acc = 0.0
    for u in units:
        out_bar_start.append(acc)
        acc += u.bars
    ov_bar0 = out_bar_start[ov]
    dunits: dict[str, list[_DeckUnit]] = {"A": [], "B": []}
    maps: dict[str, list[list[float]]] = {"A": [], "B": []}
    for ui, u in enumerate(units):
        if u.phase in ("a_ramp", "overlap"):
            s0 = ga.time(ia0 + out_bar_start[ui])
            s1 = ga.time(ia0 + out_bar_start[ui] + u.bars)
            dunits["A"].append(_DeckUnit(ui, s0, s1, 1.0))
            for q in range(max(1, int(round(u.bars)))):
                f = q / max(1, int(round(u.bars)))
                maps["A"].append([round(bounds[ui] + f * u.seconds, 4), round(s0 + f * (s1 - s0), 4)])
        if u.phase in ("overlap", "b_ramp"):
            v0 = out_bar_start[ui] - ov_bar0
            s0 = gb.time(spec.b_idx + v0 / spec.mult)
            s1 = gb.time(spec.b_idx + (v0 + u.bars) / spec.mult)
            semis = float(spec.pitch_b)
            if u.phase == "b_ramp":
                semis *= 1.0 - (u.k + 0.5) / u.n
            dunits["B"].append(_DeckUnit(ui, s0, s1, 2 ** (semis / 12)))
            for q in range(max(1, int(round(u.bars)))):
                f = q / max(1, int(round(u.bars)))
                maps["B"].append([round(bounds[ui] + f * u.seconds, 4), round(s0 + f * (s1 - s0), 4)])
    for k in ("A", "B"):
        du = dunits[k]
        maps[k].append([round(bounds[du[-1].ui + 1], 4), round(du[-1].src1, 4)])

    # Audio de origen (una sola decodificación por pista y componente) y estirado de una pasada
    use_stems = {"A": spec.a.stems is not None, "B": spec.b.stems is not None}
    comps: dict[tuple[str, str], np.ndarray] = {}
    jobs: list[tuple[str, str, Source]] = []
    for deck_id, ds in (("A", spec.a), ("B", spec.b)):
        lo = max(0.0, min(u.src0 for u in dunits[deck_id]) - CTX - 0.2)
        hi = min(max(u.src1 for u in dunits[deck_id]) + CTX + 0.2, ds.duration + 0.5)
        need = (min(u.src0 for u in dunits[deck_id]), min(max(u.src1 for u in dunits[deck_id]), ds.duration))
        sources = _load_stems(ds, lo, hi, need) if use_stems[deck_id] else _load_mix(ds, lo, hi)
        jobs += [(deck_id, name, srcobj) for name, srcobj in sources.items()]

    plans = {k: _plan_deck(dunits[k], edges) for k in ("A", "B")}
    workers = max(1, min(4, os.cpu_count() or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        lag_futs = {k: pool.submit(measure_lags, plans[k], dunits[k], edges) for k in ("A", "B")}
        lags = {k: f.result() for k, f in lag_futs.items()}
        for k, ls in lags.items():
            if max(abs(v) for v in ls) > int(0.040 * SR):
                raise RenderError(tr("el estirado de {k} sale desalineado ({ms} ms)", k=k, ms=f"{max(abs(v) for v in ls) / SR * 1000:.0f}"))

        def work(job: tuple[str, str, Source]) -> tuple[str, str, np.ndarray]:
            deck_id, name, srcobj = job
            return deck_id, name, _render_component(srcobj, dunits[deck_id], edges, total, plans[deck_id],
                                                    lags[deck_id])

        for k, (deck_id, name, arr) in enumerate(pool.map(work, jobs)):
            comps[(deck_id, name)] = arr
            if progress:
                progress((k + 1) / len(jobs))

    # Mezcla con la automatización
    t = np.arange(total, dtype=np.float32) / SR - np.float32(t_ov0)
    decks_out = []
    for deck_id, ds in (("A", spec.a), ("B", spec.b)):
        if (deck_id, "mix") in comps:
            x = comps[(deck_id, "mix")]
        else:
            x = np.zeros((total, 2), np.float32)
            for name in STEM_NAMES:
                g = envelope(lane_points(spec.lanes, deck_id, name), t, 1.0)
                x += comps[(deck_id, name)] * g[:, None]
        x = apply_eq(x, t, spec.lanes, deck_id)
        g = envelope(lane_points(spec.lanes, deck_id, "gain"), t, 1.0)
        decks_out.append(x * g[:, None] * np.float32(ds.trim))
    clip = soft_limit(decks_out[0] + decks_out[1])

    _write_wav(clip, out_dir / spec.out_name)

    return {
        "file": spec.out_name,
        "duration": round(total / SR, 4),
        "a_start": round(ga.time(ia0), 4),
        "b_resume": round(gb.time(spec.b_idx + b_len_bars), 4),
        "overlap_at": round(t_ov0, 4),
        "overlap_end": round(t_ov1, 4),
        "maps": {"a": maps["A"], "b": maps["B"]},
        "stems": bool(use_stems["A"] and use_stems["B"]),
        "engine": stretch.engine(),
        "grid_rms_ms": {"a": round(ga.rms * 1000, 1), "b": round(gb.rms * 1000, 1)},
        "render_s": round(time.time() - t_start, 2),
    }
