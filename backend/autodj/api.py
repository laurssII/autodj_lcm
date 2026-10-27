"""API local: REST para la biblioteca y el audio, WebSocket para la sesión en vivo."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import __version__, sources
from .agent import DJAgent, make_llm
from .analysis import analyzer, lyrics
from .config import settings
from .db import Database
from .i18n import tr
from .library import Library
from .models import Track
from .ratelimit import VisitorLimiter, visitor_id
from .renderer import Renderer
from .session import Session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("autodj")
logging.getLogger("httpx").setLevel(logging.WARNING)


class Hub:
    """Estado del proceso: una única sesión (app local) y sus clientes WebSocket."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = loop
        settings.ensure_dirs()
        self.db = Database(settings.db_path)
        self.library = Library(settings, self.db, notify=self._on_track)
        self.renderer = Renderer(self.library, settings)
        llm = make_llm(settings)
        n = len(llm.usable) if llm else 1
        self.session = Session(self.library, self.db, DJAgent(llm, timeout=settings.llm_timeout * max(1, n) + 5),
                               self.broadcast, renderer=self.renderer)
        self.chat_limiter = VisitorLimiter(settings.chat_per_minute, settings.chat_per_day)

    async def broadcast(self, msg: dict[str, Any]) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def _on_track(self, t: Track) -> None:
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.session.library_changed(t)))

    def import_catalog_bg(self, limit: int) -> bool:
        if self.library.importing:
            return False
        threading.Thread(target=self._safe_import, args=(limit,), daemon=True, name="catalog").start()
        return True

    def _safe_import(self, limit: int) -> None:
        try:
            self.library.import_catalog(limit)
        except Exception:
            log.exception("La importación del catálogo falló")


hub: Hub | None = None


def get_hub() -> Hub:
    assert hub is not None
    return hub


@asynccontextmanager
async def lifespan(app: FastAPI):
    global hub
    hub = Hub(asyncio.get_running_loop())
    remote = hub.db.count_tracks(["audius", "jamendo"])
    if settings.catalog_auto_import and remote < settings.catalog_min_tracks:
        log.info("Catálogo con %d pistas: importando en segundo plano…", remote)
        hub.import_catalog_bg(settings.catalog_min_tracks)
    providers = [p for p in hub.session.agent.status() if p["state"] != "sin clave"]
    log.info("AutoDJ %s listo · IA: %s · Essentia: %s · letras: %s · worker: %s", __version__,
             " → ".join(f"{p['label']} ({p['model']})" for p in providers) or "desactivada (sin claves: modo reserva)",
             analyzer.HAVE_ESSENTIA, settings.enable_lyrics and lyrics.available(), settings.worker_mode)
    yield
    hub.library.close()
    hub.db.close()


app = FastAPI(title="AutoDJ", version=__version__, lifespan=lifespan)


@app.get("/api/library/search-mainstream")
async def search_mainstream(q: str = Query("", max_length=200), limit: int = Query(8, ge=1, le=20)) -> list[dict[str, Any]]:
    """Busca coincidencias por texto. No añade nada a la biblioteca: solo las lista."""
    term = q.strip()
    if len(term) < 2:
        raise HTTPException(400, tr("Escribe al menos dos letras para buscar."))
    try:
        results = await run_in_threadpool(sources.search_mainstream, term, limit)
    except sources.MainstreamError as e:
        raise HTTPException(502, str(e)) from e
    lib = get_hub().library
    for r in results:
        existing = lib.get(r["id"])
        r["in_library"] = bool(existing and existing.status != "error")
    return results


class AddMainstreamRequest(BaseModel):
    url: str


@app.post("/api/library/add-mainstream")
async def add_mainstream(body: AddMainstreamRequest) -> dict[str, Any]:
    """Añade a la biblioteca la pista elegida en el buscador (descarga y análisis en segundo plano)."""
    h = get_hub()
    try:
        meta = await run_in_threadpool(sources.resolve_soundcloud_url, body.url.strip())
    except sources.MainstreamError as e:
        raise HTTPException(422, str(e)) from e

    track = await run_in_threadpool(h.library.add_soundcloud, meta)
    return {"status": "success", "track": track.summary() if track else None}


@app.get("/api/health")
def health() -> dict[str, Any]:
    h = get_hub()
    caps = h.renderer.caps()
    external = settings.worker_mode == "external"  # en ese modo Essentia y Whisper viven en el worker
    return {
        "ok": True, "version": __version__,
        "llm": {"enabled": h.session.agent.enabled, "chain": settings.llm_chain,
                "providers": h.session.agent.status(), "daily_budget": settings.llm_daily_budget,
                "chat_limits": {"per_minute": settings.chat_per_minute, "per_day": settings.chat_per_day}},
        "essentia": analyzer.HAVE_ESSENTIA if not external else None,
        "lyrics": bool(settings.enable_lyrics and (caps.get("lyrics") if external else lyrics.available())),
        "tracks": len(h.library.all()), "importing": h.library.importing,
        "worker": {
            "mode": settings.worker_mode,
            "alive": h.library.jobs.workers(),
            "can_render": h.renderer.can_render(),
            "can_stems": h.renderer.can_stems(),
            "pending": h.library.jobs.counts(),
        },
    }


@app.get("/api/tracks")
def list_tracks(q: str = "", source: str = "") -> list[dict[str, Any]]:
    items = get_hub().library.all()
    if source:
        items = [t for t in items if t.source == source]
    if q:
        ql = q.lower()
        items = [t for t in items if ql in f"{t.title} {t.artist} {t.eff_genre or ''}".lower()]
    items.sort(key=lambda t: (t.status not in ("ready", "needs_review"), t.artist.lower(), t.title.lower()))
    return [t.summary() for t in items]


@app.get("/api/tracks/{track_id}")
def get_track(track_id: str) -> dict[str, Any]:
    t = get_hub().library.get(track_id)
    if not t:
        raise HTTPException(404, tr("pista no encontrada"))
    d = t.to_dict()
    d.pop("beats", None)
    return d


@app.post("/api/tracks")
async def upload_track(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read()
    try:
        t = await run_in_threadpool(get_hub().library.add_upload, file.filename or "pista", data)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return t.summary()


class TrackPatch(BaseModel):
    bpm: float | None = None
    camelot: str | None = None
    genre: str | None = None


@app.patch("/api/tracks/{track_id}")
def patch_track(track_id: str, body: TrackPatch) -> dict[str, Any]:
    try:
        t = get_hub().library.update_overrides(track_id, body.model_dump(exclude_unset=True))
    except KeyError as e:
        raise HTTPException(404, tr("pista no encontrada")) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return t.summary()


@app.delete("/api/tracks/{track_id}")
def delete_track(track_id: str) -> dict[str, bool]:
    get_hub().library.delete(track_id)
    return {"ok": True}


@app.get("/api/tracks/{track_id}/audio")
async def track_audio(track_id: str, rate: float = Query(1.0, ge=0.5, le=2.0)):
    lib = get_hub().library
    t = lib.get(track_id)
    if not t:
        raise HTTPException(404, tr("pista no encontrada"))
    try:
        path = await run_in_threadpool(lib.stretched_path, t, rate)
    except Exception as e:
        raise HTTPException(502, tr("no se pudo preparar el audio: {why}", why=e)) from e
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/render/{name}")
def render_clip(name: str):
    """Clip de audio de una mezcla ya renderizada por el worker (WAV)."""
    path = get_hub().renderer.clip_path(name)
    if path is None:
        raise HTTPException(404, tr("clip no encontrado"))
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "private, max-age=3600"})


class ImportBody(BaseModel):
    limit: int = 12


@app.post("/api/catalog/import")
def import_catalog(body: ImportBody) -> dict[str, Any]:
    started = get_hub().import_catalog_bg(max(1, min(100, body.limit)))
    return {"ok": True, "started": started}


_SLOW = {"start", "chat"}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    h = get_hub()
    await ws.accept()
    h.clients.add(ws)
    visitor = visitor_id({k.lower(): v for k, v in ws.headers.items()},
                         ws.client.host if ws.client else None, settings.trusted_proxy_hops)
    await ws.send_json({"type": "hello", "logs": h.session.logs[-100:], "chat": h.session.chat_log[-100:]})
    await ws.send_json(h.session.snapshot())
    tasks: set[asyncio.Task] = set()
    try:
        while True:
            msg = await ws.receive_json()
            if not isinstance(msg, dict):
                continue
            uses_ai = msg.get("type") == "chat" or (msg.get("type") == "start" and msg.get("message"))
            if uses_ai and h.session.agent.enabled:
                ok, why = h.chat_limiter.check(visitor)
                if not ok:  # solo se le avisa a quien se ha pasado; el mensaje no llega a la IA
                    await ws.send_json({"type": "chat", "message": {
                        "id": -int(time.time() * 1000), "ts": time.time(), "role": "dj", "text": why, "via": "límite"}})
                    if msg.get("type") == "chat":
                        continue
                    msg = dict(msg, message=None)  # la sesión arranca igual, sin la sugerencia
            if msg.get("type") in _SLOW:
                task = asyncio.create_task(_safe_handle(h, msg))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            else:
                await _safe_handle(h, msg)
    except WebSocketDisconnect:
        pass
    finally:
        h.clients.discard(ws)


async def _safe_handle(h: Hub, msg: dict[str, Any]) -> None:
    try:
        await h.session.handle(msg)
    except Exception as e:
        log.exception("Error procesando %s", msg.get("type"))
        await h.broadcast({"type": "error", "message": tr("Ha ocurrido un error inesperado. Inténtalo de nuevo.")})
