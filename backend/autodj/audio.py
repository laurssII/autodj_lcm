"""Rutas de audio compartidas por la API y el worker (los dos ven el mismo directorio de datos)."""

from __future__ import annotations

import re
from pathlib import Path

from . import sources
from .config import Settings


def safe(tid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", tid)


def audio_path(settings: Settings, tid: str, source: str, stream_ref: str) -> Path:
    """Archivo de audio de una pista; si es remota y aún no está en la caché, se descarga."""
    if source == "local":
        return Path(stream_ref)
    dest = settings.audio_dir / "cache" / f"{safe(tid)}.mp3"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        sources.download(settings, source, stream_ref, dest)
    return dest
