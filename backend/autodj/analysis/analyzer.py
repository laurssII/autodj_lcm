"""Análisis de una pista: BPM, beats, downbeats, tonalidad, energía, estructura y ánimo.

Usa Essentia para tempo y tonalidad cuando está instalado (perfil `edma`, pensado para
música electrónica y de baile) y numpy en cualquier caso para el resto de rasgos.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from .. import camelot
from ..i18n import tr
from ..models import Segment
from . import dsp

log = logging.getLogger(__name__)

ANALYSIS_VERSION = "1.1"  # 1.1: rejilla de frases (8/16/32 compases), intro y outro

try:  # pragma: no cover - depende del entorno
    import essentia.standard as es  # type: ignore

    HAVE_ESSENTIA = True
except Exception:  # noqa: BLE001
    es = None
    HAVE_ESSENTIA = False


@dataclass
class AnalysisResult:
    duration: float
    bpm: float
    bpm_conf: float
    beats: list[float]
    downbeats: list[float]
    camelot: str
    key_conf: float
    energy: float
    mood: float
    loudness_lufs: float
    segments: list[Segment] = field(default_factory=list)
    engine: str = "numpy"
    phrase_offset: int = 0  # compás (0..31) donde empieza un bloque de 32; módulo 8 = fase de las frases
    intro_end: float | None = None  # s: donde acaba la intro y entra el cuerpo (en una frontera de frase)
    outro_start: float | None = None  # s: donde empieza el outro (en una frontera de frase)


def _clip01(x: float) -> float:
    return float(min(1.0, max(0.0, x)))


def _per_bar(values: np.ndarray, fps: float, downbeats: list[float], duration: float) -> np.ndarray:
    edges = list(downbeats) + [duration]
    out = []
    for a, b in zip(edges[:-1], edges[1:]):
        i0, i1 = int(a * fps), max(int(a * fps) + 1, int(b * fps))
        seg = values[i0:i1]
        out.append(float(seg.mean()) if len(seg) else 0.0)
    return np.array(out)


def _segments(bar_energy: np.ndarray, downbeats: list[float], duration: float, phrase: int = 8,
              offset: int = 0) -> list[Segment]:
    if not len(bar_energy):
        return [Segment("verse", 0.0, duration, 0.5)]
    offset %= phrase
    starts = ([0] if offset else []) + list(range(offset, len(bar_energy), phrase))
    ends = starts[1:] + [len(bar_energy)]
    pe = np.array([bar_energy[s:e].mean() for s, e in zip(starts, ends)])
    span = pe.max() - pe.min()
    e = (pe - pe.min()) / span if span > 1e-6 else np.full_like(pe, 0.5)
    hi = np.quantile(e, 0.7) if len(e) > 2 else 1.0
    labels = []
    for i, v in enumerate(e):
        if v >= hi and v > 0.55:
            labels.append("drop")
        elif v < 0.35 and 0 < i < len(e) - 1:
            labels.append("breakdown")
        else:
            labels.append("verse")
    if e[0] < 0.5:
        labels[0] = "intro"
    if len(e) > 2 and e[-1] < 0.5:
        labels[-1] = "outro"
    for i in range(1, len(labels)):
        if labels[i] == "drop" and labels[i - 1] in ("verse", "breakdown") and e[i - 1] < e[i] - 0.1:
            labels[i - 1] = "buildup"

    segs: list[Segment] = []
    for i, s in enumerate(starts):
        t0 = 0.0 if i == 0 else downbeats[s]
        t1 = downbeats[starts[i + 1]] if i + 1 < len(starts) else duration
        if segs and segs[-1].label == labels[i]:
            last = segs[-1]
            n = (last.end - last.start)
            last.energy = (last.energy * n + float(e[i]) * (t1 - t0)) / max(1e-6, n + t1 - t0)
            last.end = t1
        else:
            segs.append(Segment(labels[i], t0, t1, float(e[i])))
    for s in segs:
        s.start, s.end, s.energy = round(s.start, 3), round(s.end, 3), round(s.energy, 3)
    return segs


def _essentia_rhythm_key(path: str) -> tuple[float, np.ndarray, float, str, str, float] | None:  # pragma: no cover
    if not HAVE_ESSENTIA or not path:
        return None
    try:
        audio = es.MonoLoader(filename=path, sampleRate=44100)()
        bpm, beats, conf, _, _ = es.RhythmExtractor2013(method="multifeature")(audio)
        key, scale, strength = es.KeyExtractor(profileType="edma")(audio)
        return float(bpm), np.asarray(beats, dtype=float), float(conf) / 5.32, key, scale, float(strength)
    except Exception as e:  # noqa: BLE001
        log.warning("Essentia falló (%s); uso el análisis de respaldo", e)
        return None


def analyze_signal(y: np.ndarray, sr: int = dsp.SR, path: str | None = None) -> AnalysisResult:
    duration = len(y) / sr
    if duration < 20:
        raise ValueError(tr("la pista dura menos de 20 s"))
    sp = dsp.spectral(y, sr)
    fps = sp.fps

    ess = _essentia_rhythm_key(path) if path else None
    if ess:
        bpm, beats_s, bpm_conf, key_root, key_mode, key_conf = ess
        engine = "essentia"
    else:
        onset = sp.flux / (sp.flux.mean() + 1e-9) + 3 * sp.low_flux / (sp.low_flux.mean() + 1e-9)
        bpm, bpm_conf = dsp.estimate_tempo(onset, fps)
        frames = dsp.track_beats(onset, fps, bpm)
        beats_s = frames / fps + dsp.FRAME_OFFSET
        key_root, key_mode, key_conf = dsp.estimate_key(sp.chroma)
        engine = "numpy"

    if len(beats_s) > 8:  # tempo refinado: pendiente de la recta beat→tiempo (promedia la cuantización)
        idx = np.arange(len(beats_s))
        slope = float(np.polyfit(idx, beats_s, 1)[0])
        if slope > 0:
            bpm = 60.0 / slope
    bpm, beats_s = dsp.fold_bpm(bpm, beats_s)
    beat_frames = (beats_s - dsp.FRAME_OFFSET) * fps
    phase = dsp.downbeat_phase(beat_frames, sp.low_flux, sp.flux)
    downbeats = [round(float(t), 4) for t in beats_s[phase::4]]
    beats = [round(float(t), 4) for t in beats_s]

    code = camelot.key_to_camelot(key_root, key_mode)

    loud = dsp.integrated_loudness(y, sr)
    rms_db = 20 * np.log10(sp.rms + 1e-6)
    bar_energy = _per_bar(rms_db, fps, downbeats, duration)
    bar_low = _per_bar(sp.low_flux, fps, downbeats, duration)
    bar_amp = _per_bar(sp.rms, fps, downbeats, duration)
    bar_cent = _per_bar(sp.centroid, fps, downbeats, duration)
    phrase_offset, _ = dsp.phrase_phase(dsp.bar_novelty(bar_energy, bar_low, bar_cent))
    intro_bar, outro_bar = dsp.intro_outro_bars(bar_low, bar_amp, phrase_offset)
    intro_end = downbeats[intro_bar] if intro_bar is not None and intro_bar < len(downbeats) else None
    outro_start = downbeats[outro_bar] if outro_bar is not None and outro_bar < len(downbeats) else None
    segments = _segments(bar_energy, downbeats, duration, offset=phrase_offset)

    thr = np.median(sp.flux) * 1.5
    peaks = (sp.flux[1:-1] > thr) & (sp.flux[1:-1] > sp.flux[:-2]) & (sp.flux[1:-1] >= sp.flux[2:])
    density = peaks.sum() / duration  # onsets por segundo
    centroid = float(np.median(sp.centroid))
    energy01 = (0.5 * _clip01((loud + 20) / 14)
                + 0.3 * _clip01(density / 8)
                + 0.2 * _clip01((centroid - 800) / 2500))
    energy = round(1 + 9 * energy01, 2)

    mode_sign = -1.0 if camelot.is_minor(code) else 1.0
    mood = (0.45 * mode_sign * (0.4 + 0.6 * key_conf)
            + 0.35 * (_clip01((centroid - 800) / 2500) - 0.5) * 2
            + 0.2 * (energy01 - 0.5) * 2)

    return AnalysisResult(
        duration=round(duration, 3), bpm=round(float(bpm), 2), bpm_conf=round(_clip01(bpm_conf), 3),
        beats=beats, downbeats=downbeats, camelot=code, key_conf=round(_clip01(key_conf), 3),
        energy=energy, mood=round(float(np.clip(mood, -1, 1)), 3), loudness_lufs=round(loud, 2),
        segments=segments, engine=engine, phrase_offset=phrase_offset,
        intro_end=round(intro_end, 3) if intro_end is not None else None,
        outro_start=round(outro_start, 3) if outro_start is not None else None,
    )


def analyze_file(path: str) -> AnalysisResult:
    y = dsp.decode(path)
    return analyze_signal(y, dsp.SR, path)
