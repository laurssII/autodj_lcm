"""Modelos de datos compartidos por el análisis, el motor de decisión y la API."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Segment:
    label: str  # intro · verse · buildup · drop · breakdown · outro
    start: float
    end: float
    energy: float  # 0..1 relativa a la pista
    vocals: bool | None = None


@dataclass
class Word:
    word: str
    start: float
    end: float
    conf: float


@dataclass
class Track:
    id: str
    source: str  # audius · jamendo · local
    stream_ref: str  # id remoto o ruta del archivo
    title: str
    artist: str = ""
    genre: str | None = None
    credit_url: str | None = None
    license: str | None = None
    duration: float = 0.0
    status: str = "pending"  # pending · analyzing · ready · needs_review · error
    status_detail: str | None = None
    bpm: float | None = None
    bpm_conf: float = 0.0
    camelot: str | None = None
    key_conf: float = 0.0
    energy: float | None = None  # 1..10
    mood: float | None = None  # -1 (oscuro) .. 1 (luminoso)
    loudness_lufs: float | None = None
    beats: list[float] = field(default_factory=list)
    downbeats: list[float] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    phrase_offset: int = 0  # compás (0..31) donde empieza un bloque de 32; módulo 8 = fase de las frases
    intro_end: float | None = None  # s: fin de la intro (entra el bombo); None si no hay intro limpia
    outro_start: float | None = None  # s: inicio del outro; None si la pista acaba a pleno gas
    lyrics_words: list[Word] = field(default_factory=list)
    lyrics_status: str = "none"  # none · pending · ready · unavailable
    overrides: dict[str, Any] = field(default_factory=dict)
    analysis_version: str | None = None
    created_at: float = 0.0

    # ---- valores efectivos (las correcciones del usuario mandan) ----
    @property
    def eff_bpm(self) -> float | None:
        return self.overrides.get("bpm", self.bpm)

    @property
    def eff_camelot(self) -> str | None:
        return self.overrides.get("camelot", self.camelot)

    @property
    def eff_genre(self) -> str | None:
        return self.overrides.get("genre", self.genre)

    @property
    def playable(self) -> bool:
        return self.status in ("ready", "needs_review") and bool(self.downbeats)

    def summary(self) -> dict[str, Any]:
        """Versión ligera para listas, la interfaz y el contexto del agente."""
        return {
            "id": self.id,
            "source": self.source,
            "title": self.title,
            "artist": self.artist,
            "genre": self.eff_genre,
            "duration": round(self.duration, 1),
            "status": self.status,
            "status_detail": self.status_detail,
            "bpm": round(self.eff_bpm, 1) if self.eff_bpm else None,
            "camelot": self.eff_camelot,
            "energy": round(self.energy, 1) if self.energy is not None else None,
            "mood": round(self.mood, 2) if self.mood is not None else None,
            "credit_url": self.credit_url,
            "license": self.license,
            "lyrics": self.lyrics_status,
        }

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Track":
        d = dict(d)
        d["segments"] = [s if isinstance(s, Segment) else Segment(**s) for s in d.get("segments") or []]
        d["lyrics_words"] = [w if isinstance(w, Word) else Word(**w) for w in d.get("lyrics_words") or []]
        known = {f for f in Track.__dataclass_fields__}
        return Track(**{k: v for k, v in d.items() if k in known})


@dataclass
class TrackView:
    """Una pista vista a una velocidad de reproducción (time-stretch con key lock).

    Todos los tiempos están en segundos del audio ya estirado: t_view = t_original / rate.
    """

    track: Track
    rate: float = 1.0

    @property
    def id(self) -> str:
        return self.track.id

    @property
    def bpm(self) -> float:
        return (self.track.eff_bpm or 120.0) * self.rate

    @property
    def duration(self) -> float:
        return self.track.duration / self.rate

    @property
    def bar(self) -> float:
        return 4 * 60.0 / self.bpm

    @property
    def downbeats(self) -> list[float]:
        return [t / self.rate for t in self.track.downbeats]

    @property
    def beats(self) -> list[float]:
        return [t / self.rate for t in self.track.beats]

    def segments(self) -> list[Segment]:
        r = self.rate
        return [Segment(s.label, s.start / r, s.end / r, s.energy, s.vocals) for s in self.track.segments]

    def words(self) -> list[Word]:
        r = self.rate
        return [Word(w.word, w.start / r, w.end / r, w.conf) for w in self.track.lyrics_words]

    @property
    def intro_end(self) -> float | None:
        t = self.track.intro_end
        return None if t is None else t / self.rate

    @property
    def outro_start(self) -> float | None:
        t = self.track.outro_start
        return None if t is None else t / self.rate

    def grid(self, bars: int) -> list[float]:
        """Downbeats que abren un bloque de `bars` compases en la rejilla de frases de la pista.

        Con `bars` = 1 son todos los compases; con 8, 16 o 32 solo los que coinciden con el
        inicio de una frase de ese tamaño (según la fase detectada en el análisis).
        """
        db = self.downbeats
        if bars <= 1 or not db:
            return db
        return db[self.track.phrase_offset % bars::bars]

    def phrase_starts(self, bars_per_phrase: int = 8) -> list[float]:
        return self.grid(bars_per_phrase)
