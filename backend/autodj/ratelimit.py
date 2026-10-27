"""Límite de mensajes de chat por visitante, para que en un despliegue público nadie agote la cuota de la IA."""

from __future__ import annotations

import time
from collections import deque
from typing import Callable, Mapping

from .i18n import tr


def visitor_id(headers: Mapping[str, str], peer: str | None, trusted_hops: int) -> str:
    """IP real del visitante.

    Cada proxy de confianza añade al final de X-Forwarded-For la dirección que le habló. Con `trusted_hops`
    proxies delante del backend, la entrada en la posición -trusted_hops es la del visitante y no se puede
    falsear (lo que el cliente ponga por su cuenta queda más a la izquierda). Sin cabecera, la del socket.
    """
    xff = headers.get("x-forwarded-for") or ""
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    if trusted_hops > 0 and len(parts) >= trusted_hops:
        return parts[-trusted_hops]
    return headers.get("x-real-ip") or peer or "desconocido"


class VisitorLimiter:
    """Ventana deslizante por minuto y contador por día (UTC). 0 = sin límite."""

    def __init__(self, per_minute: int, per_day: int, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        self.per_minute, self.per_day = per_minute, per_day
        self.clock, self.wall = clock, wall
        self._recent: dict[str, deque[float]] = {}
        self._daily: dict[str, int] = {}
        self._day = ""

    def check(self, visitor: str) -> tuple[bool, str]:
        """¿Puede enviar otro mensaje? Si sí, lo cuenta. Si no, devuelve el motivo para mostrarlo."""
        day = time.strftime("%Y-%m-%d", time.gmtime(self.wall()))
        if day != self._day:
            self._day, self._daily = day, {}
        now = self.clock()
        q = self._recent.setdefault(visitor, deque())
        while q and now - q[0] >= 60:
            q.popleft()
        if self.per_day and self._daily.get(visitor, 0) >= self.per_day:
            return False, tr("Has llegado al límite de {n} mensajes por hoy. ¡Mañana más!", n=self.per_day)
        if self.per_minute and len(q) >= self.per_minute:
            wait = max(1, int(60 - (now - q[0])) + 1)
            return False, tr("Vas muy rápido: como mucho {n} mensajes por minuto. Prueba otra vez en {wait} s; la música sigue.",
                             n=self.per_minute, wait=wait)
        q.append(now)
        self._daily[visitor] = self._daily.get(visitor, 0) + 1
        if len(self._recent) > 5000:  # limpieza: visitantes sin actividad en el último minuto
            for k in [k for k, v in self._recent.items() if not v or now - v[-1] >= 60]:
                self._recent.pop(k, None)
        return True, ""
