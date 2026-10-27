"""Rueda de Camelot: conversión tonalidad ↔ código y compatibilidad armónica.

A = menor (anillo interior), B = mayor (anillo exterior). La rueda es circular:
después del 12 viene el 1.
"""

from __future__ import annotations

import re

# Nota → semitono (0 = C). Se aceptan sostenidos y bemoles.
_PITCH = {
    "C": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3, "E": 4, "F": 5,
    "F#": 6, "GB": 6, "G": 7, "G#": 8, "AB": 8, "A": 9, "A#": 10, "BB": 10, "B": 11,
}

# Semitono de la tónica → número Camelot
_MINOR = {8: 1, 3: 2, 10: 3, 5: 4, 0: 5, 7: 6, 2: 7, 9: 8, 4: 9, 11: 10, 6: 11, 1: 12}
_MAJOR = {11: 1, 6: 2, 1: 3, 8: 4, 3: 5, 10: 6, 5: 7, 0: 8, 7: 9, 2: 10, 9: 11, 4: 12}

_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]

_CODE_RE = re.compile(r"^\s*(1[0-2]|[1-9])\s*([AB])\s*$", re.IGNORECASE)


def key_to_camelot(root: str, mode: str) -> str:
    """('A', 'minor') → '8A'. Lanza ValueError si no se reconoce."""
    pc = _PITCH.get(root.strip().upper().replace("♯", "#").replace("♭", "B"))
    if pc is None:
        raise ValueError(f"tónica desconocida: {root!r}")
    m = mode.strip().lower()
    if m in ("minor", "min", "m", "menor"):
        return f"{_MINOR[pc]}A"
    if m in ("major", "maj", "mayor", ""):
        return f"{_MAJOR[pc]}B"
    raise ValueError(f"modo desconocido: {mode!r}")


def parse(code: str) -> tuple[int, str]:
    m = _CODE_RE.match(code or "")
    if not m:
        raise ValueError(f"código Camelot inválido: {code!r}")
    return int(m.group(1)), m.group(2).upper()


def is_valid(code: str | None) -> bool:
    try:
        parse(code or "")
        return True
    except ValueError:
        return False


def camelot_to_key(code: str) -> str:
    """'8A' → 'Am', '8B' → 'C'."""
    n, letter = parse(code)
    table = _MINOR if letter == "A" else _MAJOR
    pc = next(p for p, c in table.items() if c == n)
    return _NAMES[pc] + ("m" if letter == "A" else "")


def is_minor(code: str) -> bool:
    return parse(code)[1] == "A"


def _step(a: int, b: int) -> int:
    """Pasos horarios de a a b en la rueda (0..11)."""
    return (b - a) % 12


def relation(a: str, b: str) -> str:
    """Relación de b respecto a a: same, adjacent, relative, diagonal, boost2, boost7, clash."""
    na, la = parse(a)
    nb, lb = parse(b)
    step = _step(na, nb)
    if la == lb:
        if step == 0:
            return "same"
        if step in (1, 11):
            return "adjacent"
        if step == 2:
            return "boost2"
        if step == 7:
            return "boost7"
        return "clash"
    if step == 0:
        return "relative"
    # Diagonal: 8A→9B o 8B→7A (misma escala relativa desplazada una quinta)
    if (la == "A" and step == 1) or (la == "B" and step == 11):
        return "diagonal"
    return "clash"


HARMONIC_SCORE = {
    "same": 1.0,
    "adjacent": 0.9,
    "relative": 0.85,
    "diagonal": 0.7,
    "boost2": 0.6,
    "boost7": 0.5,
    "clash": 0.1,
}


def harmonic_score(a: str | None, b: str | None, conf_a: float = 1.0, conf_b: float = 1.0) -> float:
    """Puntuación armónica 0..1. Con tonalidades poco fiables tiende a 0,5."""
    if not is_valid(a) or not is_valid(b):
        return 0.5
    raw = HARMONIC_SCORE[relation(a, b)]  # type: ignore[arg-type]
    conf = max(0.0, min(1.0, min(conf_a, conf_b)))
    return 0.5 + (raw - 0.5) * conf


def compatible(a: str, b: str) -> bool:
    """Compatible para mezclas solapadas: misma, ±1, relativa o diagonal."""
    return relation(a, b) in ("same", "adjacent", "relative", "diagonal")


def transpose(code: str, semitones: int) -> str:
    """Código Camelot de la misma pista subida (o bajada) `semitones` semitonos. '8A' +1 → '3A'."""
    n, letter = parse(code)
    table = _MINOR if letter == "A" else _MAJOR
    pc = next(p for p, c in table.items() if c == n)
    return f"{table[(pc + int(semitones)) % 12]}{letter}"


def neighbours(code: str) -> list[str]:
    """Compatibles directas: misma, ±1 y cambio de anillo."""
    n, letter = parse(code)
    other = "B" if letter == "A" else "A"
    prev_n = 12 if n == 1 else n - 1
    next_n = 1 if n == 12 else n + 1
    return [f"{n}{letter}", f"{prev_n}{letter}", f"{next_n}{letter}", f"{n}{other}"]
