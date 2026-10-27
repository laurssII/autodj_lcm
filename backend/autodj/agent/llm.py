"""Clientes de LLM con tool calling y una cadena de proveedores con conmutación automática.

Cada proveedor es un cliente (API de Anthropic o cualquier API compatible con OpenAI: Groq, Gemini, GitHub
Models, Mistral, OpenRouter, Ollama…). `ChainLLM` los prueba en orden: si uno responde 429/5xx, tarda
demasiado o no está bien configurado, se le deja «enfriando» un rato y se pasa al siguiente. El resto del
sistema solo ve la interfaz `LLM` y, en cada respuesta, qué proveedor contestó.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..config import Settings
from ..i18n import tr

log = logging.getLogger(__name__)

# Proveedores conocidos: URL base (API compatible con OpenAI), variable con la clave y modelo por defecto.
PRESETS: dict[str, dict[str, str]] = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
             "model": "openai/gpt-oss-120b", "label": "Groq", "reasoning": "low"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY",
               "model": "gemini-3.5-flash-lite", "label": "Gemini", "reasoning": ""},
    "github": {"base_url": "https://models.github.ai/inference", "key_env": "GITHUB_TOKEN",
               "model": "openai/gpt-4.1-mini", "label": "GitHub Models", "reasoning": ""},
    "mistral": {"base_url": "https://api.mistral.ai/v1", "key_env": "MISTRAL_API_KEY",
                "model": "mistral-small-latest", "label": "Mistral", "reasoning": ""},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY",
                   "model": "openai/gpt-oss-20b:free", "label": "OpenRouter", "reasoning": "low"},
    "cerebras": {"base_url": "https://api.cerebras.ai/v1", "key_env": "CEREBRAS_API_KEY",
                 "model": "gpt-oss-120b", "label": "Cerebras", "reasoning": "low"},
    "ollama": {"base_url": "http://host.docker.internal:11434/v1", "key_env": "",
               "model": "qwen3:8b", "label": "Ollama", "reasoning": ""},
    "anthropic": {"base_url": "https://api.anthropic.com", "key_env": "ANTHROPIC_API_KEY",
                  "model": "claude-haiku-5-5", "label": "Anthropic", "reasoning": ""},
}

DEFAULT_CHAIN = "groq,gemini,github"


class LLMError(RuntimeError):
    """Fallo de un proveedor (o de toda la cadena).

    `status` es el código HTTP (0 = sin conexión o tiempo agotado) y `retry_after` los segundos que el
    proveedor pide esperar, si los indica.
    """

    def __init__(self, message: str, status: int = 0, retry_after: float | None = None, provider: str = "",
                 kinds: list[str] | None = None):
        super().__init__(message)
        self.kinds = kinds  # en un fallo de la cadena, el tipo de cada intento (ver `classify`)
        self.status = status
        self.retry_after = retry_after
        self.provider = provider


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]
    extra: dict[str, Any] | None = None  # datos del proveedor que hay que devolverle (firma de pensamiento de Gemini)


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: str = ""  # quién contestó: "groq:openai/gpt-oss-120b"
    latency_ms: int = 0
    attempts: list[str] = field(default_factory=list)  # fallos previos en la cadena, para el registro


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]


class LLM(Protocol):
    name: str

    async def complete(self, system: str, messages: list[dict], tools: list[Tool],
                       force_tool: str | None = None) -> LLMResponse: ...


def _httpx():
    import httpx

    return httpx


def _retry_after(headers: Any) -> float | None:
    try:
        v = headers.get("retry-after")
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _error_from(r: Any, provider: str) -> LLMError:
    text = (r.text or "")[:300].replace("\n", " ")
    return LLMError(f"{r.status_code}: {text}", r.status_code, _retry_after(r.headers), provider)


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, model: str, api_key: str, base_url: str = "", timeout: float = 20, max_tokens: int = 700):
        self.model, self.api_key, self.timeout, self.max_tokens = model, api_key, timeout, max_tokens
        self.url = (base_url.rstrip("/") or "https://api.anthropic.com") + "/v1/messages"

    @staticmethod
    def convert(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks: list[dict] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "…"}]})
            elif m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    async def complete(self, system, messages, tools, force_tool=None):
        httpx = _httpx()
        body: dict[str, Any] = {
            "model": self.model, "max_tokens": self.max_tokens, "system": system,
            "messages": self.convert(messages),
            "tools": [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools],
        }
        if force_tool:
            body["tool_choice"] = {"type": "tool", "name": force_tool}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(self.url, json=body, headers=headers)
        except Exception as e:  # noqa: BLE001
            raise LLMError(tr("sin conexión: {what}", what=type(e).__name__), 0, None, self.name) from e
        if r.status_code >= 400:
            raise _error_from(r, self.name)
        data = r.json()
        res = LLMResponse()
        for b in data.get("content", []):
            if b.get("type") == "text":
                res.text += b.get("text", "")
            elif b.get("type") == "tool_use":
                res.tool_calls.append(ToolCall(b["id"], b["name"], b.get("input") or {}))
        return res


class OpenAICompatLLM:
    name = "openai"

    def __init__(self, model: str, api_key: str, base_url: str = "", timeout: float = 20,
                 reasoning: str = "", max_tokens: int = 700):
        self.model, self.api_key, self.timeout, self.max_tokens = model, api_key, timeout, max_tokens
        self.url = (base_url.rstrip("/") or "https://api.openai.com/v1") + "/chat/completions"
        # «Modo pensar»: se pide el mínimo razonamiento; si el proveedor no entiende el parámetro (400), se deja de enviar.
        self.reasoning = reasoning
        self.force_ok = True
        self.required_ok = True

    @staticmethod
    def convert(system: str, messages: list[dict], gemini: bool = False) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = []
                    for tc in m["tool_calls"]:
                        call: dict[str, Any] = {"id": tc.id, "type": "function", "function": {
                            "name": tc.name, "arguments": json.dumps(tc.args, ensure_ascii=False)}}
                        if tc.extra:
                            call["extra_content"] = tc.extra
                        elif gemini:  # Gemini 3 exige la firma; si viene de otro proveedor, se usa la que ignora la validación
                            call["extra_content"] = {"google": {"thought_signature": "skip_thought_signature_validator"}}
                        msg["tool_calls"].append(call)
                out.append(msg)
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        return out

    @staticmethod
    def tools_payload(tools: list[Tool]) -> list[dict]:
        """Herramientas en formato OpenAI. Las que no tienen argumentos van sin `parameters`: Gemini rechaza
        (400) un objeto de parámetros vacío."""
        out = []
        for t in tools:
            fn: dict[str, Any] = {"name": t.name, "description": t.description}
            if (t.parameters or {}).get("properties"):
                fn["parameters"] = t.parameters
            out.append({"type": "function", "function": fn})
        return out

    def body(self, system: str, messages: list[dict], tools: list[Tool], force_tool: str | None) -> dict[str, Any]:
        b: dict[str, Any] = {
            "model": self.model, "messages": self.convert(system, messages, "generativelanguage.googleapis.com" in self.url),
            "temperature": 0.7,
            "max_tokens": self.max_tokens, "tools": self.tools_payload(tools),
        }
        if force_tool and self.force_ok:
            b["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
        elif force_tool and self.required_ok:
            b["tool_choice"] = "required"
        if self.reasoning:
            b["reasoning_effort"] = self.reasoning
        return b

    async def complete(self, system, messages, tools, force_tool=None):
        httpx = _httpx()
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                # Ante un 400 se reintenta quitando, por orden, los parámetros opcionales.
                for _ in range(4):
                    body = self.body(system, messages, tools, force_tool)
                    r = await c.post(self.url, json=body, headers=headers)
                    if r.status_code != 400:
                        break
                    if "reasoning_effort" in body:
                        log.info("%s rechaza reasoning_effort: se deja de enviar", self.model)
                        self.reasoning = ""
                    elif force_tool and self.force_ok:
                        log.info("%s rechaza forzar una herramienta concreta: se usa tool_choice=required", self.model)
                        self.force_ok = False
                    elif force_tool and self.required_ok:
                        log.info("%s rechaza tool_choice=required: se envía sin forzar", self.model)
                        self.required_ok = False
                    else:
                        break
        except Exception as e:  # noqa: BLE001
            raise LLMError(tr("sin conexión o tiempo agotado: {what}", what=type(e).__name__), 0, None, self.name) from e
        if r.status_code >= 400:
            raise _error_from(r, self.name)
        msg = (r.json().get("choices") or [{}])[0].get("message") or {}
        res = LLMResponse(text=msg.get("content") or "")
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            extra = tc.get("extra_content") if isinstance(tc.get("extra_content"), dict) else None
            res.tool_calls.append(ToolCall(tc.get("id") or fn.get("name", "call"), fn.get("name", ""), args, extra))
        return res


@dataclass
class Provider:
    key: str  # "groq:openai/gpt-oss-120b"
    label: str  # "Groq"
    model: str
    client: Any | None  # None = sin clave: se muestra en el estado pero no se usa
    cooldown_until: float = 0.0
    last_error: str = ""
    ok_calls: int = 0
    failed_calls: int = 0
    last_ok_at: float = 0.0


# Enfriamiento tras cada tipo de fallo (s). 429 usa el Retry-After del proveedor si lo da.
COOLDOWN = {"rate": 60.0, "daily": 3600.0, "overload": 45.0, "timeout": 30.0, "config": 900.0, "other": 60.0}


def classify(e: LLMError) -> str:
    s = e.status
    msg = str(e).lower()
    if s == 429:
        return "daily" if ("day" in msg or "daily" in msg or "per day" in msg or "quota" in msg) else "rate"
    if s in (500, 502, 503, 504, 529):
        return "overload"
    if s == 0:
        return "timeout"
    if s in (400, 401, 403, 404, 422):
        return "config"
    return "other"


def explain(e: BaseException) -> str:
    """El fallo de la IA en una frase pensada para quien usa la aplicación (sin códigos ni nombres de proveedor)."""
    if isinstance(e, asyncio.TimeoutError):
        return tr("la IA tarda demasiado en responder")
    kinds = (e.kinds if isinstance(e, LLMError) and e.kinds else None) or ([classify(e)] if isinstance(e, LLMError) else [])
    if kinds and all(k in ("rate", "daily", "overload") for k in kinds):
        return tr("la IA está saturada o ha llegado a su límite de uso")
    if kinds and all(k == "timeout" for k in kinds):
        return tr("la IA tarda demasiado en responder")
    return tr("la IA no está disponible")


class ChainLLM:
    """Prueba los proveedores en orden y salta al siguiente si uno falla o tarda.

    - `timeout`: tiempo máximo por proveedor. Mejor corto: es preferible saltar a otro que esperar.
    - `daily_budget`: tope de llamadas al día entre todos (0 = sin tope), para proteger la cuota gratuita
      en un despliegue público.
    """

    name = "chain"

    def __init__(self, providers: list[Provider], timeout: float = 8.0, daily_budget: int = 0,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time):
        self.providers = providers
        self.timeout = timeout
        self.daily_budget = daily_budget
        self.clock, self.wall = clock, wall
        self._day = ""
        self._calls_today = 0

    @property
    def usable(self) -> list[Provider]:
        return [p for p in self.providers if p.client is not None]

    def _spend(self) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(self.wall()))
        if day != self._day:
            self._day, self._calls_today = day, 0
        if self.daily_budget and self._calls_today >= self.daily_budget:
            raise LLMError(tr("presupuesto diario de llamadas agotado ({n})", n=self.daily_budget), 429, None, "chain", ["daily"])
        self._calls_today += 1

    async def complete(self, system, messages, tools, force_tool=None) -> LLMResponse:
        if not self.usable:
            raise LLMError(tr("no hay ningún proveedor de IA con clave configurada"), 0, None, "chain")
        attempts: list[str] = []
        kinds: list[str] = []
        now = self.clock()
        ready = [p for p in self.usable if p.cooldown_until <= now]
        if not ready:
            soon = min(self.usable, key=lambda p: p.cooldown_until)
            raise LLMError(tr("todos los proveedores están enfriando (el primero vuelve en {n} s)",
                              n=max(1, int(soon.cooldown_until - now))), 503, None, "chain", ["rate"])
        for p in ready:
            self._spend()
            t0 = self.clock()
            try:
                res = await asyncio.wait_for(p.client.complete(system, messages, tools, force_tool), self.timeout)
            except asyncio.TimeoutError:
                err = LLMError(tr("tardó más de {n} s", n=f"{self.timeout:g}"), 0, None, p.key)
            except LLMError as e:
                err = e
            except Exception as e:  # noqa: BLE001 — un fallo inesperado de un proveedor no tumba la cadena
                err = LLMError(tr("error inesperado: {what}: {why}", what=type(e).__name__, why=e), 0, None, p.key)
            else:
                p.ok_calls += 1
                p.last_ok_at = self.wall()
                p.last_error = ""
                res.provider = p.key
                res.latency_ms = int((self.clock() - t0) * 1000)
                res.attempts = attempts
                return res
            kind = classify(err)
            wait = COOLDOWN[kind]
            if kind == "rate" and err.retry_after:
                wait = min(max(err.retry_after, 5.0), 600.0)
            p.cooldown_until = self.clock() + wait
            p.failed_calls += 1
            p.last_error = f"{kind}: {str(err)[:160]}"
            attempts.append(f"{p.label} ({p.model}): {str(err)[:120]}")
            kinds.append(kind)
            log.warning("IA %s falló (%s); salto al siguiente. %s", p.key, kind, str(err)[:200])
        raise LLMError(tr("ningún proveedor respondió: {what}", what=" | ".join(attempts)), 503, None, "chain", kinds)

    def status(self) -> list[dict[str, Any]]:
        """Estado de cada proveedor, para la interfaz y /api/health."""
        now = self.clock()
        out = []
        for p in self.providers:
            if p.client is None:
                state = "sin clave"
            elif p.cooldown_until > now:
                state = "enfriando"
            else:
                state = "listo"
            out.append({"key": p.key, "label": p.label, "model": p.model, "state": state,
                        "cooldown_s": max(0, int(p.cooldown_until - now)) if p.cooldown_until > now else 0,
                        "last_error": p.last_error, "ok": p.ok_calls, "failed": p.failed_calls})
        return out

    def label_of(self, key: str) -> str:
        for p in self.providers:
            if p.key == key:
                return p.label
        return key


def _client(kind: str, model: str, key: str, base_url: str, timeout: float, reasoning: str):
    if kind == "anthropic":
        return AnthropicLLM(model, key, base_url, timeout)
    return OpenAICompatLLM(model, key, base_url, timeout, reasoning)


def parse_chain(spec: str) -> list[tuple[str, str]]:
    """"groq,gemini:gemini-3.5-flash-lite" → [("groq", ""), ("gemini", "gemini-3.5-flash-lite")]."""
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, model = part.partition(":")
        out.append((name.strip().lower(), model.strip()))
    return out


def build_chain(settings: Settings, env: Callable[[str], str | None] | None = None) -> ChainLLM | None:
    """Construye la cadena a partir de LLM_CHAIN y de las claves de cada proveedor.

    Compatibilidad: si existe la configuración antigua (LLM_PROVIDER + LLM_MODEL + LLM_API_KEY/LLM_BASE_URL),
    se añade como proveedor «personalizado» al final de la cadena, como último recurso.
    """
    import os

    get = env or os.getenv
    providers: list[Provider] = []
    timeout = settings.llm_timeout
    for name, model in parse_chain(settings.llm_chain):
        pre = PRESETS.get(name)
        if pre is None:
            log.warning("Proveedor de IA desconocido en LLM_CHAIN: %s", name)
            continue
        model = model or (get(f"{name.upper()}_MODEL") or pre["model"])
        key = (get(pre["key_env"]) or "") if pre["key_env"] else ""
        base = get(f"{name.upper()}_BASE_URL") or pre["base_url"]
        has_key = bool(key) or name == "ollama"
        reasoning = settings.llm_reasoning if settings.llm_reasoning != "auto" else pre["reasoning"]
        if name == "groq" and "gpt-oss" not in model and settings.llm_reasoning == "auto":
            reasoning = ""
        providers.append(Provider(
            key=f"{name}:{model}", label=pre["label"], model=model,
            client=_client(name, model, key, base, timeout, reasoning) if has_key else None))
    if settings.llm_provider in ("openai", "anthropic") and settings.llm_model:
        kind = settings.llm_provider
        reasoning = "" if settings.llm_reasoning == "auto" else settings.llm_reasoning
        providers.append(Provider(
            key=f"custom:{settings.llm_model}", label="Personalizado", model=settings.llm_model,
            client=_client(kind, settings.llm_model, settings.llm_api_key, settings.llm_base_url, timeout,
                           reasoning)))
    if not providers:
        return None
    return ChainLLM(providers, timeout=timeout, daily_budget=settings.llm_daily_budget)


def make_llm(settings: Settings) -> ChainLLM | None:
    """La cadena de proveedores (aunque ninguno tenga clave: así la interfaz puede decir qué falta).

    Sin ningún proveedor con clave el agente queda desactivado y el DJ pincha en modo reserva."""
    return build_chain(settings)