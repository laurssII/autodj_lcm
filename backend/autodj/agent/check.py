"""Comprueba, proveedor a proveedor, que la IA configurada responde y sabe usar herramientas.

Uso (con Docker):  docker compose run --rm backend python -m autodj.agent.check
Sin Docker:        cd backend && python -m autodj.agent.check

Para cada proveedor de LLM_CHAIN hace dos pruebas reales:
1. Decisión: elegir una opción con la herramienta `choose` (lo que hace el DJ en cada canción).
2. Chat: decidir por sí mismo qué herramienta usar ante una petición («pon algo de Bad Bunny»).
"""

from __future__ import annotations

import asyncio
import sys
import time

from ..config import settings
from .agent import CHAT_SYSTEM, CHAT_TOOLS, CHOOSE_TOOL, DECIDE_SYSTEM
from .llm import PRESETS, LLMError, build_chain

DECIDE_MSG = """Sonando: «Noche» — Luna · 96 BPM · 8A · energía 5 · reggaeton · quedan 1:20
Objetivo ahora: energía 7/10 · ánimo neutro · estilo de la sesión: subida progresiva
Última transición: EQ mixing

OPCIONES:
1 «Fuego» — Sol · 98 BPM · 9A (armonía 0.90) · energía 7 · reggaeton · puntuación 0.82
   1a: EQ mixing de 16 compases; empieza en el outro de la que suena; dura unos 40 s
   1b: Stems de 8 compases; empieza justo tras el primer estribillo de la que suena; dura unos 20 s
2 «Brisa» — Mar · 94 BPM · 7A (armonía 0.90) · energía 4 · pop · puntuación 0.70
   2a: Por compases de 8 compases; empieza en el outro de la que suena; dura unos 20 s"""

CHAT_MSG = """Sonando: «Noche» — Luna · 96 BPM · 8A · energía 5 · reggaeton

Mensaje del usuario:
pon algo de Bad Bunny"""


async def _check_one(p) -> tuple[bool, str]:
    client = p.client
    t0 = time.monotonic()
    try:
        res = await asyncio.wait_for(client.complete(DECIDE_SYSTEM, [{"role": "user", "content": DECIDE_MSG}],
                                                     [CHOOSE_TOOL], "choose"), 30)
    except asyncio.TimeoutError:
        return False, "decisión: más de 30 s sin respuesta"
    except LLMError as e:
        return False, f"decisión: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"decisión: error inesperado {type(e).__name__}: {e}"
    ms = int((time.monotonic() - t0) * 1000)
    calls = [tc for tc in res.tool_calls if tc.name == "choose"]
    if not calls:
        return False, f"decisión: no usó la herramienta (respondió en texto: {res.text[:80]!r})"
    opt = str(calls[0].args.get("option", "")).lower()
    if opt not in ("1a", "1b", "2a"):
        return False, f"decisión: opción no válida {opt!r}"
    decide = f"decisión OK en {ms} ms (eligió {opt}: {str(calls[0].args.get('reason', ''))[:60]})"

    t0 = time.monotonic()
    try:
        res = await asyncio.wait_for(client.complete(CHAT_SYSTEM, [{"role": "user", "content": CHAT_MSG}],
                                                     CHAT_TOOLS), 30)
    except (asyncio.TimeoutError, LLMError) as e:
        return False, f"{decide}; chat: {e or 'más de 30 s'}"
    except Exception as e:  # noqa: BLE001
        return False, f"{decide}; chat: error inesperado {type(e).__name__}: {e}"
    ms = int((time.monotonic() - t0) * 1000)
    names = [tc.name for tc in res.tool_calls]
    if not names:
        return False, f"{decide}; chat: no usó ninguna herramienta ({res.text[:60]!r})"
    return True, f"{decide}; chat OK en {ms} ms (usó {', '.join(names)})"


async def main() -> int:
    chain = build_chain(settings)
    if chain is None or not chain.providers:
        print("No hay proveedores en LLM_CHAIN.")
        return 2
    print(f"Cadena: {settings.llm_chain}  ·  tiempo máximo por proveedor en uso normal: {settings.llm_timeout:g} s\n")
    ok_any = False
    for p in chain.providers:
        name = p.key.split(":")[0]
        if p.client is None:
            env = PRESETS.get(name, {}).get("key_env", "")
            print(f"  –  {p.label:<14} {p.model:<28} sin clave ({env} vacía en .env)")
            continue
        ok, msg = await _check_one(p)
        ok_any = ok_any or ok
        print(f"  {'✓' if ok else '✗'}  {p.label:<14} {p.model:<28} {msg}")
    print()
    if ok_any:
        print("Al menos un proveedor funciona: el DJ decidirá con IA.")
        return 0
    print("Ningún proveedor funciona: el DJ pinchará en modo reserva. Revisa las claves y los modelos en .env.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
