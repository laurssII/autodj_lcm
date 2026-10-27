"""Biblioteca: alta de pistas (subidas o catálogo), cola de análisis y archivos de audio."""

from __future__ import annotations

import hashlib
import itertools
import logging
import os
import re
import subprocess
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from . import audio, jobs as jobs_mod, sources
from .analysis import ANALYSIS_VERSION, lyrics
from .config import Settings
from .i18n import get_lang, tr
from .db import Database
from .jobs import JobQueue
from .models import Segment, Track, Word

log = logging.getLogger(__name__)

AUDIO_EXT = {".mp3", ".wav", ".flac", ".aiff", ".aif", ".ogg", ".m4a", ".aac", ".opus"}


def _read_tags(path: Path) -> dict[str, str]:
    try:
        import mutagen  # type: ignore

        f = mutagen.File(str(path), easy=True)
        if not f or not f.tags:
            return {}
        return {k: str(v[0]) for k, v in f.tags.items() if v and k in ("title", "artist", "genre")}
    except Exception:  # noqa: BLE001
        return {}


def _title_from_filename(name: str) -> tuple[str, str]:
    stem = Path(name).stem.replace("_", " ").strip()
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return title.strip(), artist.strip()
    return stem, ""


class Library:
    def __init__(self, settings: Settings, db: Database, notify: Callable[[Track], None] | None = None):
        self.settings = settings
        self.db = db
        self.notify = notify or (lambda t: None)
        self._tracks: dict[str, Track] = {t.id: t for t in db.list_tracks()}
        self._lock = threading.RLock()
        # El trabajo pesado (análisis, letras) lo hace el worker; la biblioteca reparte trabajos y aplica resultados.
        self.jobs = JobQueue(db.path if db.path != ":memory:" else settings.data_dir / "jobs.sqlite")
        self.owner = uuid.uuid4().hex[:8]
        self._stretch_locks: dict[str, threading.Lock] = {}
        self._stop = threading.Event()
        self.importing = False
        self.worker = None
        settings.ensure_dirs()
        self.jobs.adopt_unseen(("analyze", "lyrics"), self.owner)  # resultados que dejó una API anterior
        # Reanaliza lo que quedó a medias en una ejecución anterior
        for t in list(self._tracks.values()):
            if t.status in ("pending", "analyzing"):
                self._enqueue(t.id, background=True)
            elif t.lyrics_status == "pending":
                self._enqueue_lyrics(t.id, background=True)
            elif t.status in ("ready", "needs_review") and not (t.analysis_version or "").startswith(
                    f"{ANALYSIS_VERSION}-"):
                # Análisis de una versión anterior: se refresca sin dejar la pista fuera de juego.
                self._enqueue(t.id, refresh=True, background=True)

        if settings.worker_mode == "inline":
            from .worker import Worker

            self.worker = Worker(settings, self.jobs).start()
        self._watcher = threading.Thread(target=self._watch, daemon=True, name="library-watcher")
        self._watcher.start()

    def close(self) -> None:
        self._stop.set()
        self._watcher.join(timeout=5)
        if self.worker:
            self.worker.stop()
        self.jobs.close()

    def all(self) -> list[Track]:
        with self._lock:
            return list(self._tracks.values())

    def get(self, tid: str) -> Track | None:
        with self._lock:
            return self._tracks.get(tid)

    def playable(self) -> list[Track]:
        return [t for t in self.all() if t.playable]

    def _save(self, t: Track) -> None:
        with self._lock:
            self._tracks[t.id] = t
        self.db.upsert_track(t)
        try:
            self.notify(t)
        except Exception:  # noqa: BLE001
            log.exception("notify falló")

    def add_upload(self, filename: str, data: bytes) -> Track:
        ext = Path(filename).suffix.lower()
        if ext not in AUDIO_EXT:
            raise ValueError(tr("formato no soportado: {ext}", ext=ext or tr("sin extensión")))
        if len(data) > self.settings.max_upload_mb * 1024 * 1024:
            raise ValueError(tr("el archivo supera {mb} MB", mb=self.settings.max_upload_mb))
        digest = hashlib.sha1(data).hexdigest()
        tid = f"local:{digest[:16]}"
        existing = self.get(tid)
        if existing and existing.status != "error":
            return existing  # duplicado: no se reanaliza
        path = self.settings.audio_dir / "local" / f"{digest[:16]}{ext}"
        path.write_bytes(data)
        tags = _read_tags(path)
        title, artist = _title_from_filename(filename)
        t = Track(id=tid, source="local", stream_ref=str(path), title=tags.get("title") or title,
                  artist=tags.get("artist") or artist, genre=tags.get("genre"))
        self._save(t)
        self._enqueue(tid)
        return t

    def add_remote(self, r: sources.RemoteTrack, background: bool = False) -> Track | None:
        """Registra una pista remota. `background=True` (catálogo) la analiza con baja prioridad."""
        if self.get(r.id):
            return None
        t = Track(id=r.id, source=r.source, stream_ref=r.stream_ref, title=r.title, artist=r.artist,
                  genre=r.genre, duration=r.duration, credit_url=r.credit_url, license=r.license)
        self._save(t)
        self._enqueue(t.id, background=background)
        return t

    def add_soundcloud(self, meta: dict) -> Track | None:
        """Añade una pista de SoundCloud ya comprobada (`sources.resolve_soundcloud_url`) y la analiza ya.

        Si había un intento anterior fallido se reintenta. Devuelve la pista (nueva o ya existente).
        """
        remote = sources.RemoteTrack(
            id=meta["id"], source="soundcloud", stream_ref=meta["url"], title=meta["title"], artist=meta["artist"],
            genre="Mainstream", duration=meta["duration"], credit_url=meta["url"],
            license="SoundCloud (uso personal)")
        track = self.add_remote(remote) or self.get(meta["id"])
        if track and track.status == "error":
            track = self.retry(track.id)
        return track

    def retry(self, tid: str) -> Track | None:
        """Vuelve a descargar y analizar una pista que quedó en error."""
        t = self.get(tid)
        if t and t.status == "error":
            t.status, t.status_detail = "pending", None
            self._save(t)
            self._enqueue(tid)
        return t

    def import_catalog(self, limit: int = 60) -> int:
        """Descubre pistas en Audius (y Jamendo si hay client_id) y las encola. Bloqueante."""
        self.importing = True
        try:
            audius = sources.audius_tracks(self.settings, limit)
            jamendo = sources.jamendo_tracks(self.settings, max(0, limit // 2))
            found = [r for pair in itertools.zip_longest(audius, jamendo) for r in pair if r][:limit]
            added = sum(1 for r in found if self.add_remote(r, background=True))
            log.info("Catálogo: %d pistas nuevas", added)
            return added
        finally:
            self.importing = False

    def update_overrides(self, tid: str, changes: dict) -> Track:
        t = self.get(tid)
        if not t:
            raise KeyError(tid)
        from . import camelot

        for k, v in changes.items():
            if v in (None, ""):
                t.overrides.pop(k, None)
            elif k == "bpm":
                t.overrides["bpm"] = float(v)
            elif k == "camelot":
                if not camelot.is_valid(str(v)):
                    raise ValueError(tr("código Camelot inválido"))
                t.overrides["camelot"] = str(v).upper()
            elif k == "genre":
                t.overrides["genre"] = str(v)
        self._save(t)
        return t

    def delete(self, tid: str) -> None:
        t = self.get(tid)
        if not t:
            return
        with self._lock:
            self._tracks.pop(tid, None)
        self.db.delete_track(tid)
        if t.source == "local":
            Path(t.stream_ref).unlink(missing_ok=True)
        for p in (self.settings.audio_dir / "stretch").glob(f"{_safe(tid)}_*"):
            p.unlink(missing_ok=True)
        shutil.rmtree(self.settings.stems_dir / _safe(tid), ignore_errors=True)

    def audio_path(self, t: Track) -> Path:
        return audio.audio_path(self.settings, t.id, t.source, t.stream_ref)

    def stretched_path(self, t: Track, rate: float) -> Path:
        """Audio a otra velocidad sin cambiar el tono (ffmpeg atempo). Se cachea en disco."""
        rate = round(rate, 3)
        src = self.audio_path(t)
        if abs(rate - 1.0) < 0.0005:
            return src
        if not 0.5 <= rate <= 2.0:
            raise ValueError("velocidad fuera de rango")
        dest = self.settings.audio_dir / "stretch" / f"{_safe(t.id)}_{rate:.3f}.mp3"
        lock = self._stretch_locks.setdefault(str(dest), threading.Lock())
        with lock:
            if not dest.exists():
                tmp = dest.with_suffix(".tmp.mp3")
                proc = subprocess.run(
                    ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(src), "-filter:a", f"atempo={rate}",
                     "-c:a", "libmp3lame", "-q:a", "3", str(tmp)], capture_output=True)
                if proc.returncode != 0:
                    raise RuntimeError(proc.stderr.decode(errors="ignore")[:300])
                tmp.replace(dest)
                self._prune_stretch(keep=dest)
            else:
                dest.touch()  # marca de uso reciente para la poda
        return dest

    def _prune_stretch(self, keep: Path) -> None:
        """Mantiene la caché de audio estirado por debajo de STRETCH_CACHE_MB (borra lo menos reciente)."""
        limit = self.settings.stretch_cache_mb * 1024 * 1024
        files = sorted((self.settings.audio_dir / "stretch").glob("*.mp3"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for p in files:
            if total <= limit:
                break
            if p == keep or ".tmp" in p.name:
                continue
            total -= p.stat().st_size
            p.unlink(missing_ok=True)

    def _payload(self, t: Track, **extra) -> dict:
        return {"tid": t.id, "source": t.source, "stream_ref": t.stream_ref, "lang": get_lang(), **extra}

    def _enqueue(self, tid: str, refresh: bool = False, background: bool = False) -> None:
        t = self.get(tid)
        if t:
            self.jobs.submit("analyze", tid, self._payload(t, refresh=refresh),
                             jobs_mod.P_BACKGROUND if background else jobs_mod.P_USER, self.owner)

    def _enqueue_lyrics(self, tid: str, background: bool = False) -> None:
        t = self.get(tid)
        if t:
            self.jobs.submit("lyrics", tid, self._payload(t),
                             jobs_mod.P_BACKGROUND if background else jobs_mod.P_USER, self.owner)

    def prioritize(self, tid: str) -> None:
        """El usuario ha pedido esta pista (cola): se analiza ya, sin esperar a las del catálogo."""
        t = self.get(tid)
        if not t:
            return
        if t.status in ("pending", "analyzing"):
            self.jobs.bump("analyze", tid, jobs_mod.P_USER)
        elif t.lyrics_status == "pending":
            self.jobs.bump("lyrics", tid, jobs_mod.P_USER)

    def _watch(self) -> None:
        while not self._stop.wait(0.2):
            try:
                self.poll()
            except Exception:  # noqa: BLE001
                log.exception("el vigilante de trabajos falló")
                self._stop.wait(1.0)

    def poll(self) -> int:
        """Aplica los resultados nuevos del worker. Devuelve cuántos trabajos ha tratado."""
        n = 0
        for job in self.jobs.unseen(("analyze", "lyrics"), self.owner):
            n += 1
            t = self.get(job.key)
            refresh = bool(job.payload.get("refresh"))
            if t is None:  # la pista se borró mientras tanto
                self.jobs.mark_seen(job.id, 2)
                continue
            if job.kind == "analyze":
                if not refresh and t.status == "pending":
                    t.status, t.status_detail = "analyzing", None
                    self._save(t)
                if job.status == jobs_mod.RUNNING:
                    self.jobs.mark_seen(job.id, 1)
                elif job.status == jobs_mod.DONE and job.result:
                    self._apply_analysis(t, job.result, refresh, job.priority >= jobs_mod.P_BACKGROUND)
                    self.jobs.mark_seen(job.id, 2)
                else:
                    log.warning("Análisis de %s falló: %s", job.key, job.error)
                    if not refresh and t.status in ("pending", "analyzing"):
                        t.status, t.status_detail = "error", (job.error or tr("el análisis falló"))[:200]
                        self._save(t)
                    self.jobs.mark_seen(job.id, 2)
            else:
                if job.status == jobs_mod.RUNNING:
                    self.jobs.mark_seen(job.id, 1)
                elif job.status == jobs_mod.DONE and job.result is not None and t.lyrics_status == "pending":
                    words = [Word(**w) for w in job.result.get("words", [])]
                    t.lyrics_words = words
                    t.segments = lyrics.mark_vocals(t.segments, words)
                    t.lyrics_status = "ready"
                    self._save(t)
                    self.jobs.mark_seen(job.id, 2)
                else:
                    if job.status == jobs_mod.FAILED:
                        log.warning("Transcripción de %s falló: %s", job.key, job.error)
                        if t.lyrics_status == "pending":
                            t.lyrics_status = "unavailable"
                            self._save(t)
                    self.jobs.mark_seen(job.id, 2)
        return n

    def _apply_analysis(self, t: Track, r: dict, refresh: bool, background: bool) -> None:
        """Pasa el análisis del worker a la pista. Con `refresh` es un reanálisis: la pista sigue reproducible."""
        if not refresh and t.status in ("ready", "needs_review"):
            return  # otro trabajo ya la analizó
        t.duration, t.bpm, t.bpm_conf = r["duration"], r["bpm"], r["bpm_conf"]
        t.beats, t.downbeats = r["beats"], r["downbeats"]
        t.camelot, t.key_conf = r["camelot"], r["key_conf"]
        t.energy, t.mood, t.loudness_lufs = r["energy"], r["mood"], r["loudness_lufs"]
        t.segments = [Segment(**s) for s in r["segments"]]
        t.phrase_offset, t.intro_end, t.outro_start = r["phrase_offset"], r["intro_end"], r["outro_start"]
        t.analysis_version = f"{ANALYSIS_VERSION}-{r['engine']}"
        low = r["bpm_conf"] < 0.25 or r["key_conf"] < 0.15
        t.status = "needs_review" if low else "ready"
        t.status_detail = tr("confianza baja en BPM o tonalidad") if low else None
        if refresh and t.lyrics_status == "ready" and t.lyrics_words:
            # La letra no depende de la rejilla: solo se rehacen las marcas de voz de los segmentos.
            t.segments = lyrics.mark_vocals(t.segments, t.lyrics_words)
        elif refresh and t.lyrics_status in ("none", "unavailable", "pending"):
            pass
        elif self.settings.enable_lyrics and (lyrics.available() or self.settings.worker_mode == "external"):
            t.lyrics_status = "pending"
        else:
            t.lyrics_status = "unavailable"
        self._save(t)
        if t.lyrics_status == "pending" and not refresh:
            self._enqueue_lyrics(t.id, background=background)


_safe = audio.safe
