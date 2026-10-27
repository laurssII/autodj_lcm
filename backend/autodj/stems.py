"""Separación de una pista en stems (batería, bajo, armonía y voz) con Demucs, solo en el worker.

Solo se separan las ventanas donde se solapan las canciones: el principio («head»), el final («tail») y
el entorno del punto de corte («cut», ver `strategies.cut_point`). Se guardan en FLAC en
`<datos>/audio/stems/<pista>/` junto con un `meta.json` que indica qué hay listo y la actividad de voz
(`vocal`, 0–1 cada `step` s).

La API solo lee `meta.json` (`window`); no importa Demucs ni torch.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Protocol

import numpy as np

from . import audio, stretch
from .config import Settings
from .i18n import tr
from .render import STEM_NAMES
from .strategies import StemWindow

log = logging.getLogger(__name__)

SR = stretch.SR
META_VERSION = 1


class Separator(Protocol):
    name: str

    def separate(self, x: np.ndarray, sr: int) -> dict[str, np.ndarray]:  # pragma: no cover - interfaz
        """x: (n, 2) float32 → un array (n, 2) por stem de STEM_NAMES."""
        ...


def available() -> bool:
    """¿Se puede usar Demucs en este proceso? (torch + demucs instalados)"""
    try:
        import demucs.apply  # noqa: F401
        import demucs.pretrained  # noqa: F401
        import torch  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


class DemucsSeparator:  # pragma: no cover - requiere torch y los pesos del modelo
    def __init__(self, model_name: str = "htdemucs", threads: int = 4):
        import torch
        from demucs.pretrained import get_model

        torch.set_num_threads(max(1, threads))
        self.name = model_name
        self.model = get_model(model_name)
        self.model.eval()
        self.sr = int(self.model.samplerate)
        if list(self.model.sources) != list(STEM_NAMES):
            raise RuntimeError(f"el modelo {model_name} devuelve {self.model.sources}, se esperaba {STEM_NAMES}")

    def separate(self, x: np.ndarray, sr: int) -> dict[str, np.ndarray]:
        import torch
        from demucs.apply import apply_model

        if sr != self.sr:
            raise ValueError(f"se esperaba audio a {self.sr} Hz")
        wav = torch.from_numpy(np.ascontiguousarray(x.T, dtype=np.float32))
        ref = wav.mean(0)
        mean, std = ref.mean(), ref.std() + 1e-8
        wav = (wav - mean) / std  # la misma normalización que usa `demucs.separate`
        with torch.no_grad():
            out = apply_model(self.model, wav[None], device="cpu", split=True, overlap=0.25, shifts=0,
                              progress=False)[0]
        out = out * std + mean
        return {n: out[i].numpy().T.astype(np.float32) for i, n in enumerate(self.model.sources)}


class BandSeparator:
    """Separador de prueba (sin IA): reparte el audio por bandas de frecuencia. Los cuatro stems suman
    exactamente la mezcla. Solo para tests y para comprobar la cadena sin Demucs."""

    name = "bands"

    def separate(self, x: np.ndarray, sr: int) -> dict[str, np.ndarray]:
        from scipy.signal import butter, sosfiltfilt

        def band(lo: float | None, hi: float | None) -> np.ndarray:
            if lo is None:
                sos = butter(2, hi, "lowpass", fs=sr, output="sos")
            elif hi is None:
                sos = butter(2, lo, "highpass", fs=sr, output="sos")
            else:
                sos = butter(2, [lo, hi], "bandpass", fs=sr, output="sos")
            return sosfiltfilt(sos, x, axis=0).astype(np.float32)

        bass = band(None, 120.0)
        drums = band(120.0, 400.0)
        other = band(400.0, 3000.0)
        vocals = (x - bass - drums - other).astype(np.float32)
        return {"drums": drums, "bass": bass, "other": other, "vocals": vocals}


_separators: dict[str, Separator] = {}


def get_separator(settings: Settings) -> Separator:
    """Separador del proceso (se carga una sola vez: cargar el modelo cuesta varios segundos)."""
    key = settings.stems_model
    if key not in _separators:
        _separators[key] = DemucsSeparator(settings.stems_model, settings.worker_threads)
    return _separators[key]


def track_dir(settings: Settings, tid: str) -> Path:
    return settings.stems_dir / audio.safe(tid)


def _read_meta(d: Path) -> dict[str, Any]:
    try:
        return json.loads((d / "meta.json").read_text())
    except (OSError, ValueError):
        return {}


def window(settings: Settings, tid: str, which: str) -> StemWindow | None:
    """Ventana de stems lista para esa pista (`head`|`tail`), o None si aún no está."""
    d = track_dir(settings, tid)
    meta = _read_meta(d)
    w = (meta.get("windows") or {}).get(which)
    if not w or meta.get("version") != META_VERSION:
        return None
    if not all((d / f"{which}_{n}.flac").exists() for n in STEM_NAMES):
        return None
    return StemWindow(str(d), which, float(w["start"]), float(w["end"]),
                      list(w["vocal"]) if w.get("vocal") else None, float(w.get("step", VOCAL_STEP)))


CUT_LEAD = 12.0  # s de la ventana `cut` antes del punto de corte (la rampa de tempo de la saliente va antes)
VOCAL_STEP = 0.5


def window_bounds(duration: float, which: str, seconds: float, center: float | None = None) -> tuple[float, float]:
    seconds = max(10.0, seconds)
    if which == "head":
        return 0.0, min(duration, seconds)
    if which == "cut":
        lo = max(0.0, (center or 0.0) - CUT_LEAD)
        return lo, min(duration, lo + max(60.0, seconds))
    return max(0.0, duration - seconds), duration


def vocal_activity(x: np.ndarray, sr: int, step: float = VOCAL_STEP) -> list[float]:
    """Energía de la voz cada `step` s, de 0 a 1 respecto a sus pasajes más fuertes (percentil 95)."""
    n = max(1, int(step * sr))
    mono = x.mean(axis=1) if x.ndim == 2 else x
    k = len(mono) // n
    if k == 0:
        return []
    rms = np.sqrt(np.mean(mono[:k * n].reshape(k, n) ** 2, axis=1))
    ref = float(np.percentile(rms, 95))
    if ref < 1e-4:
        return [0.0] * k
    return [round(float(v), 2) for v in np.clip(rms / ref, 0.0, 1.0)]


def covers(win: StemWindow | None, t0: float, t1: float) -> bool:
    return win is not None and win.start <= t0 + 1e-3 and win.end >= t1 - 1e-3


def _encode_flac(x: np.ndarray, dest: Path) -> None:
    tmp = dest.with_suffix(".tmp.flac")
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "-",
         "-c:a", "flac", "-compression_level", "3", str(tmp)],
        input=np.ascontiguousarray(np.clip(x, -1.0, 1.0), dtype=np.float32).tobytes(), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="ignore")[:300])
    tmp.replace(dest)


def separate_windows(settings: Settings, tid: str, path: str | Path, duration: float, which: list[str],
                     separator: Separator | None = None,
                     progress: Callable[[float], None] | None = None,
                     cut_at: float | None = None) -> dict[str, Any]:
    """Separa las ventanas pedidas que aún no existan. Devuelve el meta resultante."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(tr("ffmpeg no está instalado"))
    d = track_dir(settings, tid)
    d.mkdir(parents=True, exist_ok=True)
    sep = separator or get_separator(settings)
    meta = _read_meta(d)
    if meta.get("version") != META_VERSION or meta.get("model") != sep.name:
        meta = {"version": META_VERSION, "model": sep.name, "windows": {}}
        for old in d.glob("*.flac"):
            old.unlink(missing_ok=True)
    meta.setdefault("windows", {})
    meta["duration"] = duration
    todo = [w for w in which if window(settings, tid, w) is None and (w != "cut" or cut_at is not None)]
    for k, w in enumerate(todo):
        lo, hi = window_bounds(duration, w, settings.stems_window_s, cut_at)
        x = stretch.decode(str(path), lo, hi - lo)
        t0 = time.time()
        parts = sep.separate(x, SR)
        for name in STEM_NAMES:
            _encode_flac(parts[name], d / f"{w}_{name}.flac")
        meta["windows"][w] = {"start": round(lo, 3), "end": round(hi, 3), "step": VOCAL_STEP,
                              "vocal": vocal_activity(parts["vocals"], SR)}
        (d / "meta.json.tmp").write_text(json.dumps(meta))
        (d / "meta.json.tmp").replace(d / "meta.json")  # una ventana solo cuenta cuando ya están sus cuatro stems
        log.info("Stems de %s (%s, %.0f s) en %.1f s", tid, w, hi - lo, time.time() - t0)
        if progress:
            progress((k + 1) / len(todo))
    prune(settings, keep=tid)
    return meta


def prune(settings: Settings, keep: str | None = None) -> None:
    """Mantiene la caché de stems por debajo de STEMS_CACHE_MB borrando las pistas menos recientes."""
    root = settings.stems_dir
    if not root.exists():
        return
    limit = settings.stems_cache_mb * 1024 * 1024
    dirs = []
    for d in root.iterdir():
        if d.is_dir():
            size = sum(f.stat().st_size for f in d.glob("*") if f.is_file())
            meta = d / "meta.json"
            dirs.append((meta.stat().st_mtime if meta.exists() else d.stat().st_mtime, size, d))
    total = sum(s for _, s, _ in dirs)
    keep_dir = track_dir(settings, keep) if keep else None
    for _, size, d in sorted(dirs):
        if total <= limit:
            break
        if d == keep_dir:
            continue
        shutil.rmtree(d, ignore_errors=True)
        total -= size


def touch(settings: Settings, tid: str) -> None:
    """Marca las stems de esta pista como recién usadas (la poda borra primero las menos recientes)."""
    m = track_dir(settings, tid) / "meta.json"
    try:
        m.touch()
    except OSError:
        pass
