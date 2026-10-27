"""Adaptación de una pista a otra para poder mezclarlas: tempo común, doble/mitad y tonalidad.

La entrante se estira (sin cambiar el tono) hasta el tempo de la saliente mientras suenan juntas y luego
recupera el suyo con una rampa; si el hueco es grande, se reparte entre las dos. Si las tonalidades chocan,
la entrante se desplaza uno o dos semitonos durante la mezcla.

Son funciones puras: no tocan audio. El renderizador (`render.py`) las ejecuta.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import camelot

# Desviación máxima de tempo (fracción) que se admite sobre cada pista mientras suenan juntas.
MAX_DEV = 0.15
# Si el hueco entre los dos tempos es menor, la saliente no se toca y solo se estira la entrante.
FREE_GAP = 0.06
# Por debajo de esta desviación no merece la pena una rampa: el salto de tempo no se nota.
RAMP_MIN_DEV = 0.012
# Desplazamientos de tonalidad que se prueban (semitonos) y cuánto se penaliza cada uno.
MAX_SHIFT = 2
SHIFT_PENALTY = 0.05
# Una pista cuya rejilla de compases no es regular no se puede alinear con otra.
MAX_GRID_RMS = 0.040


@dataclass(frozen=True)
class TempoPlan:
    tempo: float  # tempo común (BPM, en compases de la saliente) mientras suenan juntas
    mult: float  # compases de salida que ocupa un compás de la entrante: 1, 2 o 0.5
    b_eff: float  # BPM de la entrante expresados en el dominio de la saliente (bpm_b · mult)
    gap: float  # b_eff / bpm_a − 1
    dev_a: float  # tempo / bpm_a − 1  (cuánto se toca la saliente)
    dev_b: float  # tempo / b_eff − 1  (cuánto se toca la entrante)

    @property
    def worst(self) -> float:
        return max(abs(self.dev_a), abs(self.dev_b))


def tempo_plan(bpm_a: float | None, bpm_b: float | None, max_dev: float = MAX_DEV) -> TempoPlan | None:
    """Mejor forma de igualar los tempos, o None si habría que estirar demasiado.

    Se consideran el mismo tempo, el doble y la mitad de la entrante (70 ↔ 140). Se prefiere el
    tempo sin multiplicar salvo que el doble/mitad quede mucho más cerca.
    """
    if not bpm_a or not bpm_b or bpm_a <= 0 or bpm_b <= 0:
        return None
    best: tuple[float, TempoPlan] | None = None
    for mult, bias in ((1.0, 0.0), (2.0, 0.03), (0.5, 0.03)):
        b_eff = bpm_b * mult
        gap = b_eff / bpm_a - 1
        if abs(gap) > 2 * max_dev:
            continue
        # Con un hueco pequeño la saliente no se toca; con uno grande se reparte a partes iguales.
        tempo = bpm_a if abs(gap) <= FREE_GAP else math.sqrt(bpm_a * b_eff)
        tp = TempoPlan(tempo, mult, b_eff, gap, tempo / bpm_a - 1, tempo / b_eff - 1)
        if tp.worst > max_dev + 1e-9:
            continue
        cost = tp.worst + bias
        if best is None or cost < best[0]:
            best = (cost, tp)
    return best[1] if best else None


def tempo_gap(bpm_a: float | None, bpm_b: float | None) -> float | None:
    """Hueco relativo de tempo tras elegir el mejor entre normal, doble y mitad (None si no cabe)."""
    tp = tempo_plan(bpm_a, bpm_b)
    return None if tp is None else abs(tp.gap)


@dataclass(frozen=True)
class KeyPlan:
    semitones: int  # desplazamiento de la entrante durante la mezcla (0 = ninguno)
    score: float  # puntuación armónica resultante
    base: float  # puntuación armónica sin desplazar


def _raw(a: str, b: str) -> float:
    """Compatibilidad armónica sin descontar por fiabilidad (la fiabilidad se aplica una sola vez, al final)."""
    return camelot.HARMONIC_SCORE[camelot.relation(a, b)]


# Fiabilidad mínima de las dos tonalidades para proponer un desplazamiento.
MIN_SHIFT_CONF = 0.3


def key_plan(camelot_a: str | None, camelot_b: str | None, conf_a: float = 1.0, conf_b: float = 1.0,
             max_shift: int = MAX_SHIFT) -> KeyPlan:
    """Desplazamiento de tonalidad de la entrante que mejora la armonía, si merece la pena.

    Solo se propone si las dos tonalidades son fiables y el desplazamiento convierte un choque
    o una relación floja en una compatible. Cuanto menos desplazamiento, mejor. La decisión se toma con
    la compatibilidad «de libro»; la puntuación que se devuelve sí está descontada por la fiabilidad.
    """
    base = camelot.harmonic_score(camelot_a, camelot_b, conf_a, conf_b)
    if not (camelot.is_valid(camelot_a) and camelot.is_valid(camelot_b)):
        return KeyPlan(0, base, base)
    if min(conf_a, conf_b) < MIN_SHIFT_CONF:
        return KeyPlan(0, base, base)
    raw_base = _raw(camelot_a, camelot_b)  # type: ignore[arg-type]
    if raw_base >= 0.7:
        return KeyPlan(0, base, base)
    best = KeyPlan(0, base, base)
    best_value = raw_base
    for k in range(-max_shift, max_shift + 1):
        if k == 0:
            continue
        shifted = camelot.transpose(camelot_b, k)  # type: ignore[arg-type]
        raw = _raw(camelot_a, shifted)  # type: ignore[arg-type]
        value = raw - SHIFT_PENALTY * abs(k)
        if raw >= 0.85 and value > best_value + 0.15:
            best = KeyPlan(k, camelot.harmonic_score(camelot_a, shifted, conf_a, conf_b), base)
            best_value = value
    return best


def ramp_bars(dev: float, urgent: bool = False) -> int:
    """Compases de rampa para volver al tempo original: 0 si la desviación es inapreciable."""
    d = abs(dev)
    if d < RAMP_MIN_DEV:
        return 0
    if urgent:
        return 4
    return 12 if d > 0.06 else 8


def mix_cost(bpm_a: float | None, bpm_b: float | None, camelot_a: str | None = None,
             camelot_b: str | None = None, conf_a: float = 1.0, conf_b: float = 1.0) -> float | None:
    """Coste 0..1 de mezclar de verdad A con B (None si no se puede). Para ordenar candidatas."""
    tp = tempo_plan(bpm_a, bpm_b)
    if tp is None:
        return None
    return min(1.0, tp.worst / MAX_DEV)
