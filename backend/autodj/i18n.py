"""Idioma de los textos que el backend muestra al usuario (chat, registros, errores, etiquetas).

La clave de cada texto es el propio texto en español: `tr` lo devuelve tal cual, o su traducción al
inglés si ese es el idioma activo (`catalog_en.EN`). El idioma es global al proceso porque solo hay una
sesión de DJ; lo fija el navegador con el mensaje `lang`.
"""

from __future__ import annotations

from .catalog_en import EN

LANGS = ("es", "en")
_lang = "es"


def set_lang(lang: str | None) -> str:
    global _lang
    if lang in LANGS:
        _lang = lang
    return _lang


def get_lang() -> str:
    return _lang


def tr(text: str, **params: object) -> str:
    """Texto en el idioma activo; con `params`, se rellenan sus huecos `{nombre}`."""
    out = EN.get(text, text) if _lang == "en" else text
    return out.format(**params) if params else out


def llm_directive() -> str:
    """Instrucción que se añade a los prompts (escritos en español) para que la IA conteste en el idioma activo."""
    if _lang == "en":
        return "\n\nIMPORTANT: write every reply and every reason in English, even though these instructions are in Spanish."
    return ""
