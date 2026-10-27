"""Transcripción de letras con tiempo por palabra (faster-whisper, opcional).

Se ejecuta después del análisis principal: la pista ya se puede pinchar mientras tanto,
y solo el wordplay necesita la letra.
"""

from __future__ import annotations

import logging
import threading

from ..models import Segment, Word

log = logging.getLogger(__name__)

_model = None
_lock = threading.Lock()


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _get_model(size: str, threads: int = 2):  # pragma: no cover - requiere el modelo
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel

            log.info("Cargando Whisper '%s'…", size)
            _model = WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=max(1, threads))
        return _model


def transcribe(path: str, size: str = "base", threads: int = 2) -> list[Word]:  # pragma: no cover - requiere el modelo
    model = _get_model(size, threads)
    segments, _info = model.transcribe(path, word_timestamps=True, vad_filter=True, beam_size=1)
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append(Word(text, round(w.start, 3), round(w.end, 3), round(float(w.probability), 3)))
    return words


def mark_vocals(segments: list[Segment], words: list[Word], min_words: int = 3) -> list[Segment]:
    """Marca qué segmentos tienen voz según la densidad de palabras transcritas."""
    for s in segments:
        n = sum(1 for w in words if s.start <= w.start < s.end and w.conf >= 0.4)
        s.vocals = n >= min_words
    return segments
