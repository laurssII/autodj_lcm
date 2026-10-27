"""Generador de pistas sintéticas para probar el análisis sin depender de música real."""

from __future__ import annotations

import wave

import numpy as np

NOTE = {"C": 0, "C#": 1, "D": 2, "Eb": 3, "E": 4, "F": 5, "F#": 6, "G": 7, "Ab": 8, "A": 9, "Bb": 10, "B": 11}


def freq(name: str, octave: int) -> float:
    midi = 12 * (octave + 1) + NOTE[name]
    return 440.0 * 2 ** ((midi - 69) / 12)


def make_song(bpm: float = 96.0, chords=(("A", "C", "E"), ("F", "A", "C"), ("C", "E", "G"), ("G", "B", "D")),
              tonic: str = "A", seconds: float = 90.0, sr: int = 22050, seed: int = 0,
              intro_bars: int = 0, outro_bars: int = 0) -> np.ndarray:
    """Canción sintética. Con `intro_bars` / `outro_bars` los compases de ese tramo suenan sin
    bombo ni caja (solo armonía y charles), como la intro y el outro de un tema de baile."""
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    t = np.arange(n) / sr
    y = np.zeros(n, np.float32)
    beat = 60.0 / bpm
    # Armonía: un acorde por compás, con la tónica reforzada en el bajo
    bar = 4 * beat
    for i in range(int(seconds / bar) + 1):
        s, e = int(i * bar * sr), min(n, int((i + 1) * bar * sr))
        if s >= n:
            break
        tt = t[s:e]
        chord = chords[i % len(chords)]
        for note in chord:
            y[s:e] += 0.05 * np.sin(2 * np.pi * freq(note, 4) * tt)
        y[s:e] += 0.08 * np.sin(2 * np.pi * freq(chord[0], 2) * tt)
        y[s:e] += 0.03 * np.sin(2 * np.pi * freq(tonic, 3) * tt)
    # Percusión: bombo fuerte en el 1, más suave en el 3; caja en 2 y 4; charles en corcheas
    kick_len = int(0.12 * sr)
    kt = np.arange(kick_len) / sr
    kick = np.sin(2 * np.pi * (50 + 80 * np.exp(-kt * 30)) * kt) * np.exp(-kt * 18)
    snare = rng.standard_normal(int(0.08 * sr)) * np.exp(-np.arange(int(0.08 * sr)) / sr * 35)
    hat = rng.standard_normal(int(0.02 * sr)) * np.exp(-np.arange(int(0.02 * sr)) / sr * 200)
    k = 0
    total_bars = int(seconds / bar)
    while k * beat / 2 < seconds - 0.2:
        pos = int(k * beat / 2 * sr)
        b = k // 2
        in_bar = b // 4
        bare = in_bar < intro_bars or (outro_bars > 0 and in_bar >= total_bars - outro_bars)  # sin percusión fuerte
        if k % 2 == 0 and bare:
            pass
        elif k % 2 == 0:
            if b % 4 == 0:
                y[pos:pos + kick_len] += 0.9 * kick[: n - pos]
            elif b % 4 == 2:
                y[pos:pos + kick_len] += 0.45 * kick[: n - pos]
            else:
                y[pos:pos + len(snare)] += 0.25 * snare[: n - pos]
        y[pos:pos + len(hat)] += 0.05 * hat[: n - pos]
        k += 1
    return (y / (np.abs(y).max() + 1e-9) * 0.8).astype(np.float32)


def write_wav(path: str, y: np.ndarray, sr: int = 22050) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
