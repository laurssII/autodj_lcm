"""Agente DJ: decide la siguiente canción y atiende el chat mediante herramientas."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from ..strategies import STRATEGIES
from ..i18n import llm_directive, tr
from .llm import LLM, PRESETS, LLMError, Tool

log = logging.getLogger(__name__)

STRATEGY_NAMES = list(STRATEGIES)

DECIDE_SYSTEM = """Eres AutoDJ, un DJ de formato abierto (sobre todo pop y urbano/reggaetón) que pincha en directo.
Te toca decidir la SIGUIENTE transición. Recibes el estado de la sesión y una lista de OPCIONES con id (1a, 1b, 2a…).
Cada opción es una canción y una forma concreta de mezclarla: estrategia, longitud, punto de la canción actual
donde empieza la mezcla (recortándola tras el estribillo o dejándola hasta el outro) y cómo entra la nueva.
Todas las opciones son viables: el sistema ya ha comprobado tempo, tonalidad y compases, y ejecutará exactamente
la que elijas.

Cómo decidir, por este orden:
1. Las peticiones vigentes del usuario y los objetivos de energía, ánimo y género.
2. Que el set tenga intención: subir o bajar con sentido, no saltos bruscos de energía sin motivo.
3. Variedad: no repitas la estrategia de la transición anterior ni encadenes siempre lo mismo; la opción con
   más puntuación no tiene por qué ser la mejor.
4. Calidad de la mezcla: «Stems» y «Relevo de voz» son las más limpias y vistosas; el «Wordplay» luce mucho pero
   úsalo de vez en cuando; «Mezcla corta», «Corte con eco» y «Fundido» son recursos de emergencia.
5. Recortar tras el primer estribillo mantiene el ritmo de la sesión; dejar la canción hasta el outro le da espacio.
Si solo hay opciones de una canción (viene de la cola del usuario), elige cómo mezclarla.

Responde SIEMPRE llamando a la herramienta choose con el id de la opción y un motivo: una frase corta en español,
como la explicaría un DJ (sin repetir los datos técnicos)."""

FIRST_SYSTEM = """Eres AutoDJ, un DJ de formato abierto (sobre todo pop y urbano/reggaetón). La sesión empieza:
elige la PRIMERA canción entre las opciones (id 1, 2, 3…). Busca un arranque con energía media que deje sitio
para crecer, salvo que el usuario haya pedido otra cosa. Responde llamando a la herramienta choose con el id y
un motivo breve en español."""

CHAT_SYSTEM = """Eres AutoDJ, un DJ con IA que pincha en directo en una web. Hablas con el usuario por chat, en español,
con frases cortas y naturales, como un DJ en cabina. Con cada mensaje recibes el estado de la sesión en texto.

Puedes actuar sobre la sesión SOLO con herramientas:
- search_library para encontrar canciones (siempre antes de añadir una a la cola si no tienes su id).
- Si la canción pedida NO está en la biblioteca: search_soundcloud para buscarla en SoundCloud y add_soundcloud con
  el enlace (url) del resultado que mejor coincida en título y artista. La deja en la cola; tarda unos segundos en
  descargarse y analizarse. Para que suene la siguiente, usa position 0. Si ningún resultado encaja, dilo y no añadas nada.
- queue_add / queue_remove para la cola del usuario. Las canciones de la cola suenan en orden antes que tus elecciones.
- mix_now para mezclar cuanto antes (en el siguiente inicio de frase).
- set_vibe para cambiar el rumbo: energía, tempo, ánimo (oscuro/luminoso), géneros… durante unas canciones.
- ban para vetar una canción o artista el resto de la sesión.
- set_strategy para preferir una técnica de mezcla.
- library_overview para saber qué hay en la biblioteca.

Reglas de respuesta (obligatorias):
- Contesta SIEMPRE.
- Si entiendes la petición y puedes cumplirla, hazlo con las herramientas y confirma en una línea qué vas a hacer.
- Si no la entiendes, dilo y haz una pregunta concreta.
- Si no puedes cumplirla (p. ej. no está la canción), explica por qué y propone una alternativa.
- No inventes canciones ni ids: usa solo lo que devuelvan las herramientas.
- Si el usuario adjunta una pista, normalmente quiere que suene pronto: añádela a la cola salvo que diga otra cosa."""

CHOOSE_TOOL = Tool(
    "choose",
    "Elige una de las opciones por su id.",
    {
        "type": "object",
        "properties": {
            "option": {"type": "string", "description": "id de la opción elegida, por ejemplo 2b"},
            "reason": {"type": "string", "description": "motivo breve en español, como lo diría un DJ"},
        },
        "required": ["option", "reason"],
    },
)

CHAT_TOOLS = [
    Tool("search_library", "Busca canciones por título, artista o género (búsqueda aproximada).", {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
        "required": ["query"],
    }),
    Tool("search_soundcloud", "Busca una canción en SoundCloud (solo devuelve pistas reproducibles). No añade nada.", {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "artista y título"},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 8}},
        "required": ["query"],
    }),
    Tool("add_soundcloud", "Añade a la biblioteca y a la cola una pista de SoundCloud, dado su enlace.", {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "el campo url de search_soundcloud"},
                       "position": {"type": "integer", "description": "0 = la siguiente en sonar; sin él, al final"}},
        "required": ["url"],
    }),
    Tool("queue_add", "Añade una canción a la cola del usuario (por defecto al final).", {
        "type": "object",
        "properties": {"track_id": {"type": "string"},
                       "position": {"type": "integer", "description": "0 = la siguiente en sonar"}},
        "required": ["track_id"],
    }),
    Tool("queue_remove", "Quita una canción de la cola.", {
        "type": "object", "properties": {"track_id": {"type": "string"}}, "required": ["track_id"],
    }),
    Tool("mix_now", "Mezcla a la siguiente canción cuanto antes (en el próximo inicio de frase).", {
        "type": "object", "properties": {},
    }),
    Tool("set_vibe", "Cambia el rumbo de la sesión durante varias canciones. Todos los campos son opcionales.", {
        "type": "object",
        "properties": {
            "energy": {"type": "number", "minimum": 1, "maximum": 10, "description": "energía objetivo absoluta"},
            "energy_delta": {"type": "number", "minimum": -5, "maximum": 5, "description": "subir/bajar energía"},
            "tempo_delta_pct": {"type": "number", "minimum": -15, "maximum": 15, "description": "% de tempo"},
            "mood": {"type": "number", "minimum": -1, "maximum": 1, "description": "-1 oscuro … 1 luminoso"},
            "genres": {"type": "array", "items": {"type": "string"}, "description": "géneros preferidos"},
            "avoid_genres": {"type": "array", "items": {"type": "string"}},
            "tracks": {"type": "integer", "minimum": 1, "maximum": 50, "description": "cuántas canciones dura"},
            "surprise": {"type": "number", "minimum": 0, "maximum": 1, "description": "0 previsible … 1 sorpresas"},
        },
    }),
    Tool("ban", "Veta una canción o un artista para el resto de la sesión.", {
        "type": "object",
        "properties": {"kind": {"type": "string", "enum": ["track", "artist"]}, "value": {"type": "string"}},
        "required": ["kind", "value"],
    }),
    Tool("set_strategy", "Prefiere una técnica de mezcla ('auto' para dejarlo a tu criterio).", {
        "type": "object",
        "properties": {"strategy": {"type": "string", "enum": STRATEGY_NAMES + ["auto"]}},
        "required": ["strategy"],
    }),
    Tool("library_overview", "Resumen de la biblioteca: géneros, rangos de BPM y energía, nº de pistas.", {
        "type": "object", "properties": {},
    }),
]


class Toolbox(Protocol):
    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class Choice:
    """Lo que eligió la IA y quién lo eligió."""

    option: str
    reason: str
    provider: str = ""  # "groq:openai/gpt-oss-120b"
    latency_ms: int = 0
    attempts: list[str] = field(default_factory=list)  # proveedores que fallaron antes


LogFn = Callable[[str, str, dict[str, Any] | None], Awaitable[None] | None]

_OPTION_RE = re.compile(r"\b(\d{1,2}[a-z]?)\b", re.IGNORECASE)


class DJAgent:
    def __init__(self, llm: LLM | None, log_fn: LogFn | None = None, timeout: float = 30):
        self.llm = llm
        self.log_fn = log_fn
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        if self.llm is None:
            return False
        usable = getattr(self.llm, "usable", None)
        return bool(usable) if usable is not None else True

    def status(self) -> list[dict[str, Any]]:
        fn = getattr(self.llm, "status", None)
        return fn() if callable(fn) else []

    def provider_label(self, key: str) -> str:
        fn = getattr(self.llm, "label_of", None)
        if callable(fn):
            return fn(key)
        name = key.split(":")[0] if key else ""
        return PRESETS.get(name, {}).get("label", name)

    async def _log(self, kind: str, title: str, detail: dict[str, Any] | None = None) -> None:
        if self.log_fn:
            r = self.log_fn(kind, title, detail)
            if asyncio.iscoroutine(r):
                await r

    async def choose(self, brief: str, options_block: str, valid_ids: list[str], first: bool = False) -> Choice:
        """Pide a la IA que elija una opción por su id. Lanza LLMError si no hay IA, si falla o si devuelve
        algo que no está entre `valid_ids`."""
        if not self.enabled:
            raise LLMError(tr("IA no configurada"))
        valid = {oid.lower(): oid for oid in valid_ids}
        msg = f"{brief}\n\nOPCIONES:\n{options_block}"
        system = (FIRST_SYSTEM if first else DECIDE_SYSTEM) + llm_directive()
        res = await asyncio.wait_for(
            self.llm.complete(system, [{"role": "user", "content": msg}], [CHOOSE_TOOL], "choose"), self.timeout)
        picked, reason = None, ""
        for tc in res.tool_calls:
            if tc.name == "choose":
                picked = str(tc.args.get("option") or "").strip().lower()
                reason = str(tc.args.get("reason") or "")
                break
        if picked not in valid and res.text:
            for m in _OPTION_RE.finditer(res.text):
                if m.group(1).lower() in valid:
                    picked, reason = m.group(1).lower(), reason or res.text.strip()
                    break
        if picked not in valid:
            raise LLMError(tr("la IA devolvió una opción que no existe ({what})", what=repr(picked or res.text[:60])), 422, None,
                           res.provider)
        return Choice(valid[picked], reason.strip()[:240], res.provider, res.latency_ms, list(res.attempts))

    async def chat(self, text: str, brief: str, toolbox: Toolbox, max_steps: int = 5) -> tuple[str, str]:
        """Conversa usando herramientas. Devuelve (respuesta, proveedor que la dio)."""
        if not self.enabled:
            raise LLMError(tr("IA no configurada"))
        messages: list[dict] = [{"role": "user", "content": f"{brief}\n\nMensaje del usuario:\n{text}"}]
        last_results: list[str] = []
        provider = ""
        for _ in range(max_steps):
            res = await asyncio.wait_for(self.llm.complete(CHAT_SYSTEM + llm_directive(), messages, CHAT_TOOLS), self.timeout)
            provider = res.provider or provider
            if not res.tool_calls:
                return res.text.strip() or self._fallback_reply(last_results), provider
            messages.append({"role": "assistant", "content": res.text, "tool_calls": res.tool_calls})
            for tc in res.tool_calls:
                try:
                    out = await toolbox.call(tc.name, tc.args)
                except Exception as e:  # noqa: BLE001
                    out = {"ok": False, "error": str(e)[:200]}
                await self._log("tool", tr("Herramienta {name}", name=tc.name),
                                {"args": tc.args, "result": out, "provider": res.provider})
                last_results.append(f"{tc.name}: {out.get('message') or ('ok' if out.get('ok', True) else 'error')}")
                messages.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name,
                                 "content": json.dumps(out, ensure_ascii=False)[:3000]})
        return self._fallback_reply(last_results), provider

    @staticmethod
    def _fallback_reply(results: list[str]) -> str:
        if results:
            return tr("Hecho: {what}", what="; ".join(results[-3:]))
        return tr("No he podido entender bien la petición. ¿Me la dices de otra forma?")
