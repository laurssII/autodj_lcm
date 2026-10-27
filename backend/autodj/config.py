"""Configuración leída de variables de entorno (ver .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "si", "sí", "on")


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("AUTODJ_DATA_DIR", "./data")))

    # IA
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "none").lower())
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", ""))
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", ""))
    # Tiempo máximo por proveedor: corto, porque si uno tarda es mejor saltar al siguiente de la cadena.
    llm_timeout: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT", "8")))
    # Cadena de proveedores en orden, «proveedor» o «proveedor:modelo» separados por comas.
    # Cada proveedor lee su clave de su propia variable (GROQ_API_KEY, GEMINI_API_KEY, GITHUB_TOKEN…).
    llm_chain: str = field(default_factory=lambda: os.getenv("LLM_CHAIN", "groq,gemini,github"))
    # Nivel de razonamiento pedido a los modelos que «piensan». auto = el mínimo útil de cada proveedor.
    llm_reasoning: str = field(default_factory=lambda: os.getenv("LLM_REASONING", "auto").lower())
    # Tope de llamadas a la IA al día entre todos los visitantes (0 = sin tope). Protege la cuota gratuita.
    llm_daily_budget: int = field(default_factory=lambda: int(os.getenv("LLM_DAILY_BUDGET", "0")))
    # Opciones que se le dan a la IA en cada decisión: canciones candidatas y variantes de mezcla por canción.
    ai_candidates: int = field(default_factory=lambda: int(os.getenv("AI_CANDIDATES", "5")))
    ai_variants: int = field(default_factory=lambda: int(os.getenv("AI_VARIANTS", "3")))

    # Límite de mensajes de chat por visitante (despliegue público). 0 = sin límite.
    chat_per_minute: int = field(default_factory=lambda: int(os.getenv("CHAT_PER_MINUTE", "6")))
    chat_per_day: int = field(default_factory=lambda: int(os.getenv("CHAT_PER_DAY", "60")))
    # Cuántos proxies de confianza hay delante del backend (el nginx del frontend cuenta como 1). Sirve para
    # sacar la IP real del visitante de X-Forwarded-For sin que un cliente pueda falsearla.
    trusted_proxy_hops: int = field(default_factory=lambda: int(os.getenv("TRUSTED_PROXY_HOPS", "1")))

    # Fuentes
    audius_app_name: str = field(default_factory=lambda: os.getenv("AUDIUS_APP_NAME", "AutoDJ"))
    audius_api_key: str = field(default_factory=lambda: os.getenv("AUDIUS_API_KEY", ""))
    audius_bearer_token: str = field(default_factory=lambda: os.getenv("AUDIUS_BEARER_TOKEN", ""))
    jamendo_client_id: str = field(default_factory=lambda: os.getenv("JAMENDO_CLIENT_ID", ""))
    spotify_client_id: str = field(default_factory=lambda: os.getenv("SPOTIFY_CLIENT_ID", ""))
    spotify_client_secret: str = field(default_factory=lambda: os.getenv("SPOTIFY_CLIENT_SECRET", ""))
    catalog_auto_import: bool = field(default_factory=lambda: _bool("CATALOG_AUTO_IMPORT", True))
    # Pistas que se importan al arrancar con la biblioteca vacía. Pocas: cada una se descarga y analiza.
    catalog_min_tracks: int = field(default_factory=lambda: int(os.getenv("CATALOG_MIN_TRACKS", "12")))

    # Análisis
    analysis_workers: int = field(default_factory=lambda: int(os.getenv("ANALYSIS_WORKERS", "2")))
    # Las pistas del catálogo (segundo plano) se analizan con baja prioridad: las primeras `analysis_burst`
    # a ritmo normal y, a partir de ahí, con una pausa de `analysis_cooldown_s` entre una y otra.
    # Las que pide el usuario (adjuntar, buscar en la web, cola) se analizan enseguida.
    analysis_burst: int = field(default_factory=lambda: int(os.getenv("ANALYSIS_BURST", "4")))
    analysis_cooldown_s: float = field(default_factory=lambda: float(os.getenv("ANALYSIS_COOLDOWN_S", "20")))
    whisper_threads: int = field(default_factory=lambda: int(os.getenv("WHISPER_THREADS", "2")))
    enable_lyrics: bool = field(default_factory=lambda: _bool("ENABLE_LYRICS", True))
    whisper_model: str = field(default_factory=lambda: os.getenv("WHISPER_MODEL", "base"))
    max_upload_mb: int = field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_MB", "60")))
    # Tope de la caché de pistas con el tempo cambiado; se borran las menos usadas al superarlo.
    stretch_cache_mb: int = field(default_factory=lambda: int(os.getenv("STRETCH_CACHE_MB", "300")))

    # Mezcla adaptativa y worker
    # inline: el worker corre como hilos dentro de la API (desarrollo, tests). external: es otro
    # proceso (`python -m autodj.worker`, servicio `worker` de docker-compose) y la API solo reparte trabajo.
    worker_mode: str = field(default_factory=lambda: os.getenv("AUTODJ_WORKER", "inline").lower())
    mix_adaptive: bool = field(default_factory=lambda: _bool("MIX_ADAPTIVE", True))
    # Recortar las canciones: tras el primer estribillo se empieza a mezclar durante el verso siguiente.
    mix_shorten: bool = field(default_factory=lambda: _bool("MIX_SHORTEN", True))
    stems_enabled: bool = field(default_factory=lambda: _bool("STEMS_ENABLED", True))
    # Segundos del principio y del final de cada pista que se separan en stems.
    stems_window_s: float = field(default_factory=lambda: float(os.getenv("STEMS_WINDOW_S", "80")))
    stems_cache_mb: int = field(default_factory=lambda: int(os.getenv("STEMS_CACHE_MB", "1500")))
    stems_model: str = field(default_factory=lambda: os.getenv("STEMS_MODEL", "htdemucs"))
    render_keep: int = field(default_factory=lambda: int(os.getenv("RENDER_KEEP", "12")))
    render_timeout_s: float = field(default_factory=lambda: float(os.getenv("RENDER_TIMEOUT_S", "90")))
    worker_threads: int = field(default_factory=lambda: int(os.getenv("WORKER_THREADS", "4")))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "autodj.sqlite"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def llm_enabled(self) -> bool:
        """Hay al menos un proveedor de IA con clave (ver agent/llm.py: build_chain)."""
        from .agent.llm import build_chain  # noqa: PLC0415 (evita un ciclo de importación)

        chain = build_chain(self)
        return bool(chain and chain.usable)

    @property
    def render_dir(self) -> Path:
        return self.audio_dir / "render"

    @property
    def stems_dir(self) -> Path:
        return self.audio_dir / "stems"

    def ensure_dirs(self) -> None:
        for sub in ("local", "cache", "stretch", "render", "stems"):
            (self.audio_dir / sub).mkdir(parents=True, exist_ok=True)


settings = Settings()
