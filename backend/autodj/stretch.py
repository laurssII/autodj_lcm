"""Cambio de tempo y de tono sobre arrays de audio, con ffmpeg + Rubber Band.

Admite cambiar tempo y tono mientras procesa (`asendcmd`), lo que permite rampas de tempo en un solo
proceso continuo. El desfase que deja Rubber Band lo mide y compensa el renderizador (`render.measure_lags`).
"""

from __future__ import annotations

import functools
import shutil
import subprocess

import numpy as np

from .i18n import tr

SR = 44100


class StretchError(RuntimeError):
    pass


@functools.lru_cache(maxsize=1)
def have_rubberband() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True,
                             timeout=20).stdout
    except Exception:  # noqa: BLE001
        return False
    return any(line.split()[1:2] == ["rubberband"] for line in out.splitlines() if line.strip())


def engine() -> str:
    return "rubberband" if have_rubberband() else "none"


def _filter(tempo: float, pitch: float, formant: bool) -> str:
    opts = f"tempo={tempo:.7f}:pitch={pitch:.7f}:transients=mixed:detector=compound"
    if formant:
        opts += ":formant=preserved:pitchq=quality"
    return f"rubberband={opts}"


def stretch_scheduled(x: np.ndarray, schedule: list[tuple[float, float, float]], sr: int = SR) -> np.ndarray:
    """Estira `x` (n, 2) con un calendario de (instante de ENTRADA en s, tempo, tono).

    El primer elemento fija el ajuste inicial; los demás se aplican al llegar el audio a ese
    instante. `tempo` > 1 acelera; `pitch` es un factor de frecuencia (1 = sin cambio).
    """
    if x.ndim != 2 or x.shape[1] != 2:
        raise ValueError("se espera audio estéreo (n, 2)")
    if not schedule:
        raise ValueError("calendario vacío")
    if len(x) == 0:
        return np.zeros((0, 2), np.float32)
    if not have_rubberband():
        raise StretchError("este ffmpeg no incluye el filtro rubberband")
    _, tempo0, pitch0 = schedule[0]
    cmds: list[str] = []
    cur_t, cur_p = tempo0, pitch0
    for t, tempo, pitch in schedule[1:]:
        if abs(tempo - cur_t) > 1e-7:
            cmds.append(f"{t:.5f} rubberband tempo {tempo:.7f}")
        if abs(pitch - cur_p) > 1e-7:
            cmds.append(f"{t:.5f} rubberband pitch {pitch:.7f}")
        cur_t, cur_p = tempo, pitch
    formant = any(abs(p - 1.0) > 1e-7 for _, _, p in schedule)
    af = _filter(tempo0, pitch0, formant)
    if cmds:
        af = f"asendcmd=c='{';'.join(cmds)}'," + af
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-f", "f32le", "-ar", str(sr), "-ac", "2", "-i", "-",
         "-af", af, "-f", "f32le", "-"],
        input=np.ascontiguousarray(x, dtype=np.float32).tobytes(), capture_output=True, check=False)
    if proc.returncode != 0 or not proc.stdout:
        raise StretchError(proc.stderr.decode(errors="ignore")[:300] or tr("ffmpeg no devolvió audio"))
    y = np.frombuffer(proc.stdout, dtype=np.float32)
    return y[: len(y) // 2 * 2].reshape(-1, 2).copy()


def stretch(x: np.ndarray, tempo: float, pitch: float = 1.0, sr: int = SR) -> np.ndarray:
    """Estira con un tempo y un tono constantes. Salida de exactamente `round(len(x) / tempo)` muestras."""
    n_out = int(round(len(x) / tempo))
    if len(x) == 0:
        return np.zeros((0, 2), np.float32)
    if abs(tempo - 1.0) < 1e-6 and abs(pitch - 1.0) < 1e-6:
        return x.copy()
    y = stretch_scheduled(x, [(0.0, tempo, pitch)], sr)
    if len(y) < n_out:
        y = np.vstack([y, np.zeros((n_out - len(y), 2), np.float32)])
    return y[:n_out].copy()


@functools.lru_cache(maxsize=64)
def _channels(path: str) -> int:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                              "stream=channels", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return int(out.split(",")[0])
    except Exception:  # noqa: BLE001
        return 2


def decode(path: str, start: float = 0.0, duration: float | None = None, sr: int = SR) -> np.ndarray:
    """Decodifica un tramo de un archivo a estéreo float32 (n, 2). Fuera del archivo, silencio."""
    if not shutil.which("ffmpeg"):
        raise StretchError(tr("ffmpeg no está instalado"))
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.4f}"]
    cmd += ["-i", path]
    if duration is not None:
        cmd += ["-t", f"{duration:.4f}"]
    if _channels(path) == 1:  # mono → estéreo sin los −3 dB que aplica `-ac 2`: igual que el navegador
        cmd += ["-af", "pan=stereo|c0=c0|c1=c0"]
    else:
        cmd += ["-ac", "2"]
    cmd += ["-ar", str(sr), "-f", "f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise StretchError(f"no se pudo decodificar {path}: {proc.stderr.decode(errors='ignore')[:300]}")
    y = np.frombuffer(proc.stdout, dtype=np.float32)
    y = y[: len(y) // 2 * 2].reshape(-1, 2)
    if duration is not None:
        want = int(round(duration * sr))
        if len(y) < want:
            y = np.vstack([y, np.zeros((want - len(y), 2), np.float32)])
        y = y[:want]
    return y.copy()
