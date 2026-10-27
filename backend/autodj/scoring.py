"""Candidatas para la siguiente canción: reglas fijas, puntuación y sorteo ponderado."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from . import adapt, camelot
from .models import Track
from .strategies import RenderCtx, tempo_delta, viable_strategies

WEIGHTS = {"harmony": 0.35, "tempo": 0.25, "energy": 0.20, "vibe": 0.10, "novelty": 0.10}


@dataclass
class Targets:
    """Objetivos vigentes de la sesión (los ajusta el agente según el chat)."""

    energy: float = 6.0  # 1..10
    bpm_shift: float = 0.0  # fracción deseada respecto al tempo actual (+0.05 = 5 % más rápido)
    mood: float | None = None  # -1..1
    genres: list[str] = field(default_factory=list)
    avoid_genres: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    track: Track
    score: float
    parts: dict[str, float]
    strategies: list[str]

    def describe(self) -> dict[str, Any]:
        s = self.track.summary()
        return {
            "id": s["id"], "title": s["title"], "artist": s["artist"], "genre": s["genre"],
            "bpm": s["bpm"], "camelot": s["camelot"], "energy": s["energy"], "mood": s["mood"],
            "score": round(self.score, 3),
            "parts": {k: round(v, 2) for k, v in self.parts.items()},
            "strategies": self.strategies,
        }


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def tempo_score(bpm_a: float | None, bpm_b: float | None, shift: float = 0.0, adaptive: bool = False) -> float:
    if not bpm_a or not bpm_b:
        return 0.5
    target = bpm_a * (1 + shift)
    if adaptive:
        # Con mezcla adaptativa casi todo cabe: puntúa cuánto hay que estirar (menos es mejor).
        tp = adapt.tempo_plan(target, bpm_b)
        if tp is None:
            return 0.0
        return 1.0 if tp.worst <= 0.02 else 1.0 - 0.5 * min(1.0, tp.worst / adapt.MAX_DEV)
    d = tempo_delta(target, bpm_b)
    if d <= 0.02:
        return 1.0
    return max(0.0, 1 - (d - 0.02) / 0.10)


def energy_score(energy: float | None, target: float) -> float:
    if energy is None:
        return 0.5
    return max(0.0, 1 - abs(energy - target) / 9)


def vibe_score(t: Track, targets: Targets) -> float:
    parts = []
    g = _norm(t.eff_genre)
    if targets.genres:
        parts.append(1.0 if any(x in g or g in x for x in map(_norm, targets.genres) if g) else 0.2)
    if targets.mood is not None and t.mood is not None:
        parts.append(1 - abs(t.mood - targets.mood) / 2)
    return sum(parts) / len(parts) if parts else 0.5


def hard_filter(t: Track, *, played: set[str], recent_artists: list[str], banned_tracks: set[str],
                banned_artists: set[str], failed: set[str], targets: Targets) -> bool:
    if not t.playable or t.id in played or t.id in banned_tracks or t.id in failed:
        return False
    a = _norm(t.artist)
    if a and (a in {_norm(x) for x in recent_artists[-3:]} or a in banned_artists):
        return False
    g = _norm(t.eff_genre)
    if g and any(_norm(x) in g for x in targets.avoid_genres):
        return False
    return True


def harmony_score(current: Track, t: Track, adaptive: bool = False) -> float:
    base = camelot.harmonic_score(current.eff_camelot, t.eff_camelot, current.key_conf, t.key_conf)
    if not adaptive:
        return base
    kp = adapt.key_plan(current.eff_camelot, t.eff_camelot, current.key_conf, t.key_conf)
    return max(base, kp.score - adapt.SHIFT_PENALTY * abs(kp.semitones)) if kp.semitones else base


def score(current: Track | None, rate: float, t: Track, targets: Targets,
          pair_counts: dict[tuple[str, str], int], adaptive: bool = False) -> tuple[float, dict[str, float]]:
    if current is None:
        parts = {"harmony": 0.5, "tempo": 0.5,
                 "energy": energy_score(t.energy, targets.energy),
                 "vibe": vibe_score(t, targets), "novelty": 1.0}
    else:
        bpm_a = (current.eff_bpm or 0) * rate
        n = pair_counts.get((current.id, t.id), 0)
        parts = {
            "harmony": harmony_score(current, t, adaptive),
            "tempo": tempo_score(bpm_a, t.eff_bpm, targets.bpm_shift, adaptive),
            "energy": energy_score(t.energy, targets.energy),
            "vibe": vibe_score(t, targets),
            "novelty": 1.0 / (1 + n),
        }
    total = sum(WEIGHTS[k] * v for k, v in parts.items())
    return total, parts


def candidates(current: Track | None, rate: float, library: list[Track], targets: Targets,
               pair_counts: dict[tuple[str, str], int], limit: int = 10, ctx: RenderCtx | None = None,
               **filters: Any) -> list[Candidate]:
    out: list[Candidate] = []
    adaptive = bool(ctx and ctx.render and abs(rate - 1.0) < 1e-3)
    for t in library:
        if current is not None and t.id == current.id:
            continue
        if not hard_filter(t, targets=targets, **filters):
            continue
        s, parts = score(current, rate, t, targets, pair_counts, adaptive)
        strats = viable_strategies(current, t, rate, ctx) if current else []
        if current is not None and not strats:
            s *= 0.6
        out.append(Candidate(t, s, parts, strats))
    out.sort(key=lambda c: c.score, reverse=True)
    return out[:limit]


def weighted_pick(cands: list[Candidate], temperature: float, rng: random.Random) -> Candidate | None:
    """Sorteo ponderado: con temperatura alta hay más sorpresas."""
    if not cands:
        return None
    t = max(0.01, temperature)
    best = max(c.score for c in cands)
    weights = [math.exp((c.score - best) / t) for c in cands]
    return rng.choices(cands, weights=weights, k=1)[0]
