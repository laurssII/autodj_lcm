"""Fuentes de música: Audius, Jamendo y búsqueda en SoundCloud (para uso personal, en local).

Solo se usan para descubrir pistas y descargar su audio una vez (para analizarlo y
servirlo al navegador). Se guarda siempre el enlace al original para acreditar al artista.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .i18n import tr

log = logging.getLogger(__name__)

# Bases de las APIs (no las páginas web: audius.co y jamendo.com devuelven HTML, no JSON).
AUDIUS_API = "https://api.audius.co/v1"
JAMENDO_API = "https://api.jamendo.com/v3.0"

AUDIUS_GENRES = ["Pop", "Latin", "Hip-Hop/Rap", "R&B/Soul", "Dance & EDM", "Electronic", "Reggae"]
JAMENDO_TAGS = ["pop", "reggaeton", "latin", "dance", "hiphop", "rnb"]


@dataclass
class RemoteTrack:
    id: str  # "audius:<id>" · "jamendo:<id>" · "soundcloud:<id>"
    source: str
    stream_ref: str
    title: str
    artist: str
    genre: str | None
    duration: float
    credit_url: str | None
    license: str | None


def _client(settings: Settings):
    import httpx  

    headers = {"User-Agent": "AutoDJ/0.1 (+portfolio project)"}
    if settings.audius_api_key:
        headers["x-api-key"] = settings.audius_api_key
    if settings.audius_bearer_token:
        headers["Authorization"] = f"Bearer {settings.audius_bearer_token}"
    return httpx.Client(timeout=30, follow_redirects=True, headers=headers)


def audius_tracks(settings: Settings, limit: int = 60) -> list[RemoteTrack]:
    out: dict[str, RemoteTrack] = {}
    with _client(settings) as c:
        for genre in AUDIUS_GENRES:
            if len(out) >= limit:
                break
            try:
                r = c.get(f"{AUDIUS_API}/tracks/trending",
                          params={"genre": genre, "time": "allTime", "app_name": settings.audius_app_name})
                r.raise_for_status()
                data = r.json().get("data") or []
            except Exception as e:  # incluye respuestas que no son JSON
                log.warning("Audius (%s) no responde: %s", genre, e)
                continue
            log.info("Audius (%s): %d pistas", genre, len(data))
            for t in data:
                dur = float(t.get("duration") or 0)
                if not t.get("is_streamable", True) or not (90 <= dur <= 420):
                    continue
                tid = f"audius:{t['id']}"
                permalink = t.get("permalink") or ""
                out.setdefault(tid, RemoteTrack(
                    id=tid, source="audius", stream_ref=str(t["id"]), title=t.get("title") or "Sin título",
                    artist=(t.get("user") or {}).get("name") or "", genre=t.get("genre") or genre,
                    duration=dur, credit_url=f"https://audius.co{permalink}" if permalink else None,
                    license="Audius (acreditar al artista)",
                ))
    return list(out.values())[:limit]


def audius_stream_url(settings: Settings, ref: str) -> str:
    return f"{AUDIUS_API}/tracks/{ref}/stream?app_name={settings.audius_app_name}"


def jamendo_tracks(settings: Settings, limit: int = 40) -> list[RemoteTrack]:
    if not settings.jamendo_client_id:
        log.info("Jamendo desactivado: falta JAMENDO_CLIENT_ID en el .env (es gratis en devportal.jamendo.com)")
        return []
    out: dict[str, RemoteTrack] = {}
    with _client(settings) as c:
        for tag in JAMENDO_TAGS:
            if len(out) >= limit:
                break
            try:
                r = c.get(f"{JAMENDO_API}/tracks/", params={
                    "client_id": settings.jamendo_client_id, "format": "json", "limit": 20,
                    "tags": tag, "order": "popularity_total", "audioformat": "mp32", "include": "musicinfo",
                })
                r.raise_for_status()
                results = r.json().get("results") or []
            except Exception as e:  # incluye respuestas que no son JSON
                log.warning("Jamendo (%s) no responde: %s", tag, e)
                continue
            log.info("Jamendo (%s): %d pistas", tag, len(results))
            for t in results:
                dur = float(t.get("duration") or 0)
                if not t.get("audio") or not (90 <= dur <= 420):
                    continue
                genres = ((t.get("musicinfo") or {}).get("tags") or {}).get("genres") or [tag]
                tid = f"jamendo:{t['id']}"
                out.setdefault(tid, RemoteTrack(
                    id=tid, source="jamendo", stream_ref=t["audio"], title=t.get("name") or "Sin título",
                    artist=t.get("artist_name") or "", genre=genres[0], duration=dur,
                    credit_url=t.get("shareurl"), license=t.get("license_ccurl"),
                ))
    return list(out.values())[:limit]


SOUNDCLOUD_HOSTS = ("soundcloud.com", "snd.sc")


class MainstreamError(RuntimeError):
    """Fallo al buscar o resolver una pista de SoundCloud; el mensaje se puede enseñar al usuario."""


def is_soundcloud_url(url: str) -> bool:
    """Solo se aceptan enlaces de SoundCloud: yt-dlp entiende cientos de sitios (y archivos locales)."""
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme in ("http", "https") and any(host == h or host.endswith("." + h) for h in SOUNDCLOUD_HOSTS)


def _friendly(err: Exception) -> str:
    msg = str(err)
    if "DRM" in msg:
        return tr("Esta pista tiene protección DRM (SoundCloud Go+) y no se puede usar.")
    return msg.replace("ERROR: ", "")[:300]


def _thumbnail(info: dict[str, Any]) -> str | None:
    if info.get("thumbnail"):
        return info["thumbnail"]
    urls = [t.get("url") for t in info.get("thumbnails") or [] if isinstance(t, dict) and t.get("url")]
    for marker in ("t200x200", "large"):  # tamaño razonable para una miniatura de 40 px
        for u in urls:
            if marker in u:
                return u
    return urls[0] if urls else None


def _soundcloud_meta(info: dict[str, Any]) -> dict[str, Any] | None:
    url = info.get("webpage_url") or info.get("url")
    if info.get("id") in (None, "") or not url:
        return None
    return {
        "id": f"soundcloud:{info['id']}",
        "title": info.get("title") or "Sin título",
        "artist": info.get("uploader") or info.get("artist") or "Artista desconocido",
        "duration": float(info.get("duration") or 0),
        "url": url,
        "thumbnail": _thumbnail(info),
    }


_TTL = 600.0  # s: lo ya buscado o comprobado no vuelve a pedirse a SoundCloud
_CACHE_MAX = 64
_EXTRA_CANDIDATES = 4  # se comprueban algunas más de las que se muestran, por si se descartan
_search_cache: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}
_resolved: dict[str, tuple[float, dict[str, Any]]] = {}  # url → metadatos de pistas ya comprobadas
_lock = threading.Lock()


class _QuietLogger:
    """yt-dlp imprime cada pista con DRM como un ERROR; aquí son descartes normales."""

    def debug(self, msg: str) -> None: ...
    def info(self, msg: str) -> None: ...
    def warning(self, msg: str) -> None: ...

    def error(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)


def _cache_get(cache: dict, key: Any) -> Any | None:
    with _lock:
        hit = cache.get(key)
        if hit and time.monotonic() - hit[0] < _TTL:
            return hit[1]
        cache.pop(key, None)
    return None


def _cache_put(cache: dict, key: Any, value: Any) -> None:
    with _lock:
        if len(cache) >= _CACHE_MAX:
            cache.pop(min(cache, key=lambda k: cache[k][0]), None)
        cache[key] = (time.monotonic(), value)


def search_mainstream(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Busca en SoundCloud y devuelve hasta `limit` pistas que SÍ se pueden reproducir.

    1. Una sola petición "plana" lista las coincidencias (unas pocas más de las pedidas).
    2. Cada una se comprueba en paralelo: las que tienen DRM (SoundCloud Go+) o solo ofrecen una
       previsualización se descartan, porque yt-dlp no puede descargarlas.
    Nada se descarga. Lanza MainstreamError si la búsqueda falla del todo.
    """
    import yt_dlp

    limit = max(1, min(20, int(limit)))
    key = (query.strip().lower(), limit)
    cached = _cache_get(_search_cache, key)
    if cached is not None:
        return [dict(r) for r in cached]

    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "ignoreerrors": True,
                "extract_flat": "in_playlist", "socket_timeout": 10, "logger": _QuietLogger()}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"scsearch{limit + _EXTRA_CANDIDATES}:{query}", download=False)
    except Exception as e:  # noqa: BLE001
        log.error("Error buscando en SoundCloud con yt-dlp: %s", e)
        raise MainstreamError(tr("No se pudo buscar en SoundCloud: {why}", why=_friendly(e))) from e

    urls: list[str] = []
    for entry in (info or {}).get("entries") or []:
        url = (entry or {}).get("webpage_url") or (entry or {}).get("url")
        if url and is_soundcloud_url(url) and url not in urls:
            urls.append(url)
    if not urls:
        return []

    with ThreadPoolExecutor(max_workers=min(12, len(urls))) as pool:
        metas = list(pool.map(_try_resolve, urls))  # conserva el orden de relevancia
    out: dict[str, dict[str, Any]] = {}
    for meta in metas:
        if meta:
            out.setdefault(meta["id"], meta)
    results = list(out.values())[:limit]
    log.info("SoundCloud «%s»: %d coincidencias, %d reproducibles", query, len(urls), len(results))
    if results:
        _cache_put(_search_cache, key, [dict(r) for r in results])
    return results


def _try_resolve(url: str) -> dict[str, Any] | None:
    try:
        return resolve_soundcloud_url(url)
    except MainstreamError:
        return None


def resolve_soundcloud_url(url: str) -> dict[str, Any]:
    """Comprueba que una pista de SoundCloud se puede reproducir y devuelve sus metadatos.

    Lanza MainstreamError si no es un enlace de SoundCloud, si la pista tiene DRM o si solo ofrece
    una previsualización de 30 s.
    """
    if not is_soundcloud_url(url):
        raise MainstreamError(tr("Solo se pueden añadir enlaces de SoundCloud."))
    import yt_dlp

    cached = _cache_get(_resolved, url)
    if cached is not None:
        return dict(cached)
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
                               "socket_timeout": 10, "logger": _QuietLogger()}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        log.debug("No se pudo resolver %s: %s", url, e)
        raise MainstreamError(_friendly(e)) from e
    info = info or {}
    meta = _soundcloud_meta(info) if not info.get("entries") else None
    if not meta:
        raise MainstreamError(tr("Ese enlace no es una pista de SoundCloud."))
    formats = info.get("formats") or []
    if formats and all("/preview" in (f.get("url") or "") for f in formats):
        raise MainstreamError(tr("Esta pista solo ofrece una previsualización de 30 s (SoundCloud Go+)."))
    _cache_put(_resolved, url, dict(meta))
    _cache_put(_resolved, meta["url"], dict(meta))
    return meta


def download_soundcloud(ref: str, dest: Path) -> Path:
    """Descarga el audio de una pista de SoundCloud y lo deja como mp3 en `dest`.

    `ref` es la URL de la pista. Por compatibilidad también acepta el formato antiguo
    "artista|||título", que se resuelve con una búsqueda.
    """
    import yt_dlp

    if ref.startswith(("http://", "https://")):
        if not is_soundcloud_url(ref):
            raise ValueError("enlace no permitido (solo SoundCloud)")
        target = ref
    else:
        artist, _, title = ref.partition("|||")
        target = f"scsearch1:{artist} - {title}".strip()

    stem = dest.with_suffix("")
    tmp_mp3 = Path(f"{stem}.dl.mp3")
    for old in dest.parent.glob(f"{stem.name}.dl.*"):  # restos de una descarga interrumpida
        old.unlink(missing_ok=True)

    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "outtmpl": f"{stem}.dl.%(ext)s",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(target, download=True)
        if not tmp_mp3.exists():
            raise RuntimeError(tr("yt-dlp no generó el archivo de audio"))
        tmp_mp3.replace(dest)
    finally:
        for left in dest.parent.glob(f"{stem.name}.dl.*"):
            left.unlink(missing_ok=True)
    return dest


def download(settings: Settings, source: str, ref: str, dest: Path) -> Path:
    if source == "soundcloud":
        return download_soundcloud(ref, dest)

    url = audius_stream_url(settings, ref) if source == "audius" else ref
    tmp = dest.with_suffix(dest.suffix + f".{os.getpid()}.{threading.get_ident()}.part")  # API y worker pueden bajar a la vez
    with _client(settings) as c, c.stream("GET", url) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                f.write(chunk)
    tmp.replace(dest)
    return dest
