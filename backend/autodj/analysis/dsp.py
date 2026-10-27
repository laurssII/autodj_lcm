"""DSP en numpy: decodificación, onsets, tempo, beats, tonalidad y sonoridad.

Es la implementación de respaldo cuando Essentia no está disponible, y también la que
calcula las características que Essentia no da directamente (graves por compás, brillo…).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

import numpy as np

from ..i18n import tr

SR = 22050
N_FFT = 2048
HOP = 512
# Un frame se asocia al centro de su ventana: así los beats caen sobre el golpe real.
FRAME_OFFSET = N_FFT / 2 / SR


def decode(path: str, sr: int = SR) -> np.ndarray:
    """Decodifica cualquier formato que entienda ffmpeg a mono float32."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError(tr("ffmpeg no está instalado"))
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
        capture_output=True, check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"no se pudo decodificar el audio: {proc.stderr.decode(errors='ignore')[:300]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


@dataclass
class Spectral:
    flux: np.ndarray  # novedad espectral de banda completa por frame
    low_flux: np.ndarray  # novedad en graves (< 150 Hz): bombo
    rms: np.ndarray
    centroid: np.ndarray
    chroma: np.ndarray  # (12,) media de la pista
    fps: float


def spectral(y: np.ndarray, sr: int = SR) -> Spectral:
    win = np.hanning(N_FFT).astype(np.float32)
    n_frames = max(1, 1 + (len(y) - N_FFT) // HOP)
    freqs = np.fft.rfftfreq(N_FFT, 1 / sr)
    low_bins = freqs < 150
    chroma_bins = (freqs >= 55) & (freqs <= 2000)
    midi = 69 + 12 * np.log2(np.maximum(freqs[chroma_bins], 1e-6) / 440.0)
    pcs = np.round(midi).astype(int) % 12

    flux = np.zeros(n_frames, np.float32)
    low_flux = np.zeros(n_frames, np.float32)
    rms = np.zeros(n_frames, np.float32)
    centroid = np.zeros(n_frames, np.float32)
    chroma = np.zeros(12, np.float64)
    prev = None
    chunk = 1024
    for c0 in range(0, n_frames, chunk):
        idx = np.arange(c0, min(n_frames, c0 + chunk))
        frames = np.stack([y[i * HOP:i * HOP + N_FFT] for i in idx])
        if frames.shape[1] < N_FFT:
            frames = np.pad(frames, ((0, 0), (0, N_FFT - frames.shape[1])))
        mag = np.abs(np.fft.rfft(frames * win, axis=1)).astype(np.float32)
        logm = np.log1p(100 * mag)
        diff = np.diff(np.vstack([logm[:1] if prev is None else prev[None], logm]), axis=0)
        pos = np.maximum(diff, 0)
        flux[idx] = pos.sum(axis=1)
        low_flux[idx] = pos[:, low_bins].sum(axis=1)
        prev = logm[-1]
        power = mag ** 2
        rms[idx] = np.sqrt(power.mean(axis=1) * 2 / N_FFT)
        tot = power.sum(axis=1) + 1e-9
        centroid[idx] = (power * freqs).sum(axis=1) / tot
        np.add.at(chroma, pcs, power[:, chroma_bins].sum(axis=0))
    return Spectral(flux, low_flux, rms, centroid, chroma, sr / HOP)


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1:
        return x
    k = np.hanning(n + 2)[1:-1]
    return np.convolve(x, k / k.sum(), mode="same")


def estimate_tempo(onset: np.ndarray, fps: float, lo: float = 70, hi: float = 180) -> tuple[float, float]:
    """Tempo por autocorrelación de la envolvente de onsets, con un prior centrado en 110 BPM."""
    x = onset - onset.mean()
    n = len(x)
    if n < 16:
        return 120.0, 0.0
    f = np.fft.rfft(x, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    ac /= ac[0] + 1e-9
    lags = np.arange(n)
    bpm = np.where(lags > 0, 60 * fps / np.maximum(lags, 1), 0)
    mask = (bpm >= lo) & (bpm <= hi)
    if not mask.any():
        return 120.0, 0.0
    prior = np.exp(-0.5 * (np.log2(np.maximum(bpm, 1) / 110.0) / 0.9) ** 2)
    # Peine: el periodo real también correlaciona a 2, 3 y 4 beats (el compás).
    comb = np.zeros_like(ac)
    for k, w in ((1, 1.0), (2, 0.5), (3, 0.25), (4, 0.6)):
        m = n // k
        comb[:m] += w * ac[::k][:m]
    score = np.where(mask, comb * prior, -np.inf)
    best = int(np.argmax(score))
    if 1 <= best < n - 1:
        a, b, c = ac[best - 1], ac[best], ac[best + 1]
        denom = a - 2 * b + c
        shift = 0.5 * (a - c) / denom if denom != 0 else 0.0
        lag = best + float(np.clip(shift, -0.5, 0.5))
    else:
        lag = float(best)
    conf = float(np.clip(ac[best] / (np.max(ac[mask]) + 1e-9), 0, 1)) * float(np.clip(ac[best] * 3, 0, 1))
    return 60 * fps / lag, conf


def track_beats(onset: np.ndarray, fps: float, bpm: float, tightness: float = 100.0) -> np.ndarray:
    """Seguimiento de beats por programación dinámica (Ellis, 2007). Devuelve frames."""
    period = 60 * fps / bpm
    env = _smooth(onset / (onset.std() + 1e-9), 3)
    n = len(env)
    score = env.copy()
    back = np.full(n, -1)
    lo, hi = int(round(period / 2)), int(round(period * 2))
    for t in range(lo, n):
        p0, p1 = max(0, t - hi), t - lo
        if p1 <= p0:
            continue
        prevs = np.arange(p0, p1)
        pen = -tightness * np.log((t - prevs) / period) ** 2
        cand = score[p0:p1] + pen
        j = int(np.argmax(cand))
        score[t] = env[t] + cand[j]
        back[t] = p0 + j
    tail = score[max(0, n - int(period)):]
    t = int(np.argmax(tail)) + max(0, n - int(period))
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    return np.array(beats[::-1], dtype=float)


def fold_bpm(bpm: float, beats: np.ndarray, lo: float = 70, hi: float = 140) -> tuple[float, np.ndarray]:
    """Lleva el tempo al rango [lo, hi) quitando o intercalando beats."""
    while bpm >= hi and len(beats) > 2:
        bpm /= 2
        beats = beats[::2]
    while bpm < lo and len(beats) > 1:
        bpm *= 2
        mids = (beats[:-1] + beats[1:]) / 2
        beats = np.sort(np.concatenate([beats, mids]))
    return bpm, beats


def downbeat_phase(beat_frames: np.ndarray, low_flux: np.ndarray, flux: np.ndarray) -> int:
    """Fase (0..3) cuyos beats tienen más bombo: el primer tiempo del compás."""
    idx = np.clip(beat_frames.astype(int), 0, len(flux) - 1)
    lf = low_flux / (low_flux.mean() + 1e-9)
    fl = flux / (flux.mean() + 1e-9)
    scores = [lf[idx[p::4]].mean() + 0.3 * fl[idx[p::4]].mean() if len(idx[p::4]) else 0 for p in range(4)]
    return int(np.argmax(scores))


def bar_novelty(*series: np.ndarray, span: int = 2) -> np.ndarray:
    """Cuánto cambia la música en cada compás: diferencia entre los `span` compases anteriores
    y los `span` siguientes, sumada sobre varias series (energía, graves, brillo) normalizadas.

    Un inicio de frase es justo donde entra o sale material nuevo, así que aquí los picos
    marcan las fronteras estructurales.
    """
    n = len(series[0])
    out = np.zeros(n)
    for s in series:
        s = np.asarray(s, dtype=float)
        s = (s - s.mean()) / (s.std() + 1e-9)
        for i in range(1, n):
            before = s[max(0, i - span):i].mean()
            after = s[i:min(n, i + span)].mean()
            out[i] += abs(after - before)
    # Se afila el pico restando la media de los vecinos.
    padded = np.pad(out, 1)
    return np.maximum(0.0, out - 0.5 * (padded[:-2] + padded[2:]))


def _phase_scores(novelty: np.ndarray, period: int, candidates: list[int]) -> np.ndarray:
    scores = []
    for p in candidates:
        idx = np.arange(p, len(novelty), period)
        idx = idx[idx >= 1]  # el compás 0 no tiene "antes": no puede puntuar
        scores.append(float(novelty[idx].mean()) if len(idx) else 0.0)
    return np.array(scores)


def phrase_phase(novelty: np.ndarray, period: int = 8, block: int = 32) -> tuple[int, float]:
    """Compás (0..block-1) en el que empieza un bloque de `block` compases.

    `fase % 8` dice dónde empiezan las frases de 8 compases; `fase % 16` y `fase` entera
    resuelven cuáles de esas frases abren un bloque de 16 y de 32. Devuelve también la
    confianza (0..1); con poca evidencia se devuelve la fase 0 y confianza 0.
    """
    n = len(novelty)
    if n < period * 2:
        return 0, 0.0
    s8 = _phase_scores(novelty, period, list(range(period)))
    base = float(s8.mean()) + 1e-9
    p8 = int(np.argmax(s8))
    conf = float(np.clip((s8[p8] - base) / base, 0.0, 1.0))
    if conf < 0.25:
        return 0, conf
    phase = p8
    if n >= block * 2:  # hay material para distinguir entre las frases de 8 compases
        cands = [p8 + period * k for k in range(block // period)]
        s32 = _phase_scores(novelty, block, cands)
        if s32.max() > s32.mean() * 1.15:
            phase = cands[int(np.argmax(s32))]
    return phase, conf


def _best_step(act: np.ndarray, lo: int, hi: int, win: int, rising: bool) -> tuple[int | None, float, float]:
    """Compás de [lo, hi) con el mayor cambio de nivel sostenido: media de los `win` compases
    siguientes menos la de los `win` anteriores (o al revés si `rising` es False).

    Devuelve (compás, tamaño del escalón, nivel medio del lado más bajo).
    """
    n = len(act)
    best: tuple[int | None, float, float] = (None, 0.0, 1.0)
    for i in range(max(lo, 2), min(hi, n - 1)):
        before, after = act[max(0, i - win):i], act[i:min(n, i + win)]
        if len(before) < 2 or len(after) < 2:
            continue
        step = (after.mean() - before.mean()) if rising else (before.mean() - after.mean())
        if step > best[1]:
            best = (i, float(step), float(min(before.mean(), after.mean())))
    return best


def intro_outro_bars(bar_low: np.ndarray, bar_amp: np.ndarray, phase: int, period: int = 8,
                     min_bars: int = 4, min_step: float = 0.15) -> tuple[int | None, int | None]:
    """Compás en que acaba la intro (el cuerpo de la canción entra con toda su fuerza) y en que
    empieza el outro (la actividad cae y ya no vuelve).

    La "actividad" de cada compás mezcla la presencia de bombo (novedad en graves) y el volumen,
    ambos relativos al cuerpo de la canción. La intro y el outro son el escalón de actividad más
    marcado al principio y al final. Ambos se ajustan a la frase más cercana.

    Devuelven None si no hay un escalón claro (de al menos `min_step` del nivel del cuerpo) o si
    el tramo dura menos de `min_bars` compases: entonces no hay un tramo limpio donde apoyar la
    mezcla y las estrategias usan su alternativa.
    """
    n = len(bar_low)
    if n < period * 3:
        return None, None
    lo, amp = np.asarray(bar_low, float), np.asarray(bar_amp, float)
    body = slice(n // 4, (3 * n) // 4)
    body_low, body_amp = float(np.median(lo[body])), float(np.median(amp[body]))
    if body_low <= 1e-9 or body_amp <= 1e-9:
        return None, None
    act = 0.5 * lo / body_low + 0.5 * amp / body_amp  # ≈ 1 en el cuerpo de la canción
    ph = phase % period

    def snap(i: int) -> int:
        return max(0, ph + period * round((i - ph) / period))

    i, step, low_level = _best_step(act, min_bars, n // 2, period, rising=True)
    intro_bar = snap(i) if i is not None and step >= min_step and low_level < 0.9 else None
    if intro_bar is not None and intro_bar < min_bars:
        intro_bar = None

    j, step, low_level = _best_step(act, n // 2, n - min_bars + 1, period, rising=False)
    outro_bar = snap(j) if j is not None and step >= min_step and low_level < 0.9 else None
    if outro_bar is not None and (outro_bar >= n or n - outro_bar < min_bars):
        outro_bar = None
    return intro_bar, outro_bar


_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


def estimate_key(chroma: np.ndarray) -> tuple[str, str, float]:
    """Tonalidad por correlación con los perfiles de Krumhansl-Kessler."""
    c = chroma / (chroma.sum() + 1e-12)
    scores = []
    for mode, prof in (("major", _MAJOR), ("minor", _MINOR)):
        for root in range(12):
            r = np.corrcoef(c, np.roll(prof, root))[0, 1]
            scores.append((float(r), _NAMES[root], mode))
    scores.sort(reverse=True)
    best, second = scores[0], scores[1]
    conf = float(np.clip((best[0] - second[0]) * 6 + 0.25 * best[0], 0, 1))
    return best[1], best[2], conf


def integrated_loudness(y: np.ndarray, sr: int = SR) -> float:
    """Aproximación de LUFS integrados (bloques de 400 ms, umbral absoluto y relativo)."""
    block = int(0.4 * sr)
    hop = block // 4
    if len(y) < block:
        return -70.0
    n = 1 + (len(y) - block) // hop
    ms = np.array([np.mean(y[i * hop:i * hop + block] ** 2) for i in range(n)])
    lk = -0.691 + 10 * np.log10(ms + 1e-12)
    g = ms[lk > -70]
    if not len(g):
        return -70.0
    rel = -0.691 + 10 * np.log10(g.mean()) - 10
    g2 = ms[lk > max(-70, rel)]
    return float(-0.691 + 10 * np.log10(g2.mean() + 1e-12))
