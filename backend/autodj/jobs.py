"""Cola de trabajos entre la API y el worker, en la misma base SQLite (modo WAL).

La API reparte trabajo (analizar, transcribir, renderizar una mezcla, separar stems) y el worker, que puede
ser otro proceso, lo ejecuta y deja el resultado en la tabla. Cada hilo abre su propia conexión y las
reclamaciones usan `BEGIN IMMEDIATE`, así que dos workers nunca cogen el mismo trabajo.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Prioridades: menor número = antes.
P_RENDER = 0
P_USER = 1
P_NEAR = 5  # stems de lo que va a sonar pronto
P_BACKGROUND = 10  # catálogo

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
ACTIVE = (QUEUED, RUNNING)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    payload TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 5,
    status TEXT NOT NULL DEFAULT 'queued',
    result TEXT,
    error TEXT,
    owner TEXT,
    worker TEXT,
    seen INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    progress REAL NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    started REAL,
    finished REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, priority, id);
CREATE INDEX IF NOT EXISTS idx_jobs_key ON jobs(kind, key, status);
CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    pid INTEGER,
    host TEXT,
    lanes TEXT,
    caps TEXT,
    beat REAL NOT NULL
);
"""


@dataclass
class Job:
    id: int
    kind: str
    key: str
    payload: dict[str, Any]
    priority: int
    status: str
    result: dict[str, Any] | None
    error: str | None
    owner: str | None
    seen: int
    progress: float
    attempts: int

    @staticmethod
    def from_row(r: sqlite3.Row) -> "Job":
        return Job(r["id"], r["kind"], r["key"], json.loads(r["payload"]), r["priority"], r["status"],
                   json.loads(r["result"]) if r["result"] else None, r["error"], r["owner"], r["seen"],
                   r["progress"], r["attempts"])


class JobQueue:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("la cola de trabajos necesita una base de datos en archivo")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        c = self._conn()
        c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)  # transacciones a mano
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def _tx(self, fn):
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            out = fn(c)
            c.execute("COMMIT")
            return out
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def submit(self, kind: str, key: str, payload: dict[str, Any], priority: int = P_USER,
               owner: str | None = None) -> int:
        """Encola un trabajo. Si ya hay uno igual (mismo tipo y clave) en cola o en marcha, lo reutiliza
        (sube su prioridad si la nueva es mayor y pasa a ser suyo) y devuelve su id."""

        def run(c: sqlite3.Connection) -> int:
            row = c.execute("SELECT id, priority FROM jobs WHERE kind=? AND key=? AND status IN ('queued','running') "
                            "ORDER BY id LIMIT 1", (kind, key)).fetchone()
            if row:
                c.execute("UPDATE jobs SET priority=MIN(priority, ?), owner=COALESCE(?, owner) "
                          "WHERE id=? ", (priority, owner, row["id"]))
                return int(row["id"])
            cur = c.execute("INSERT INTO jobs(kind,key,payload,priority,owner,created) VALUES (?,?,?,?,?,?)",
                            (kind, key, json.dumps(payload), priority, owner, time.time()))
            return int(cur.lastrowid)

        return self._tx(run)

    def bump(self, kind: str, key: str, priority: int) -> bool:
        """Sube la prioridad de un trabajo que aún no ha empezado."""
        def run(c: sqlite3.Connection) -> bool:
            cur = c.execute("UPDATE jobs SET priority=? WHERE kind=? AND key=? AND status='queued' AND priority>?",
                            (priority, kind, key, priority))
            return cur.rowcount > 0

        return self._tx(run)

    def cancel(self, job_id: int) -> None:
        self._tx(lambda c: c.execute("UPDATE jobs SET status='cancelled', finished=? WHERE id=? AND status='queued'",
                                     (time.time(), job_id)))

    def claim(self, kinds: Iterable[str], worker: str, min_priority: int = 0,
              max_priority: int = 1000) -> Job | None:
        ks = list(kinds)
        marks = ",".join("?" * len(ks))

        def run(c: sqlite3.Connection) -> Job | None:
            row = c.execute(
                f"SELECT * FROM jobs WHERE status='queued' AND kind IN ({marks}) AND priority BETWEEN ? AND ? "
                "ORDER BY priority, id LIMIT 1", (*ks, min_priority, max_priority)).fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET status='running', worker=?, started=?, attempts=attempts+1 WHERE id=?",
                      (worker, time.time(), row["id"]))
            return Job.from_row(c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

        return self._tx(run)

    def progress(self, job_id: int, value: float) -> None:
        self._tx(lambda c: c.execute("UPDATE jobs SET progress=? WHERE id=?", (float(value), job_id)))

    def complete(self, job_id: int, result: dict[str, Any] | None) -> None:
        self._tx(lambda c: c.execute(
            "UPDATE jobs SET status='done', result=?, finished=?, progress=1 WHERE id=?",
            (json.dumps(result) if result is not None else None, time.time(), job_id)))

    def fail(self, job_id: int, error: str) -> None:
        self._tx(lambda c: c.execute("UPDATE jobs SET status='failed', error=?, finished=? WHERE id=?",
                                     (error[:500], time.time(), job_id)))

    def requeue_stale(self, max_age: float = 60.0) -> int:
        """Devuelve a la cola los trabajos 'en marcha' de un worker que dejó de dar señales."""
        def run(c: sqlite3.Connection) -> int:
            cur = c.execute(
                "UPDATE jobs SET status='queued', worker=NULL FROM (SELECT j.id AS jid FROM jobs j "
                "LEFT JOIN workers w ON w.id=j.worker WHERE j.status='running' AND "
                "(w.id IS NULL OR w.beat < ?)) WHERE jobs.id=jid", (time.time() - max_age,))
            return cur.rowcount

        return self._tx(run)

    def get(self, job_id: int) -> Job | None:
        row = self._conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def find(self, kind: str, key: str) -> Job | None:
        """Último trabajo de ese tipo y clave."""
        row = self._conn().execute("SELECT * FROM jobs WHERE kind=? AND key=? ORDER BY id DESC LIMIT 1",
                                   (kind, key)).fetchone()
        return Job.from_row(row) if row else None

    def unseen(self, kinds: Iterable[str], owner: str | None = None) -> list[Job]:
        """Trabajos con algo nuevo que la API aún no ha aplicado: ya en marcha (seen 0) o terminados (seen < 2)."""
        ks = list(kinds)
        marks = ",".join("?" * len(ks))
        rows = self._conn().execute(
            f"SELECT * FROM jobs WHERE kind IN ({marks}) AND ((status='running' AND seen=0) OR "
            "(status IN ('done','failed') AND seen<2)) ORDER BY id", ks).fetchall()
        return [Job.from_row(r) for r in rows if owner is None or r["owner"] in (None, owner)]

    def mark_seen(self, job_id: int, level: int) -> None:
        self._tx(lambda c: c.execute("UPDATE jobs SET seen=MAX(seen, ?) WHERE id=?", (level, job_id)))

    def adopt_unseen(self, kinds: Iterable[str], owner: str) -> int:
        """Una API recién arrancada se hace cargo de lo que dejó terminado otra instancia."""
        ks = list(kinds)
        marks = ",".join("?" * len(ks))

        def run(c: sqlite3.Connection) -> int:
            return c.execute(f"UPDATE jobs SET owner=? WHERE kind IN ({marks}) AND status IN ('queued','running','done','failed') "
                             "AND seen<2 AND (owner IS NULL OR owner<>?)", (owner, *ks, owner)).rowcount

        return self._tx(run)

    def busy(self, kind: str) -> bool:
        """¿Hay algún trabajo de ese tipo en cola o en marcha?"""
        row = self._conn().execute("SELECT 1 FROM jobs WHERE kind=? AND status IN ('queued','running') LIMIT 1",
                                   (kind,)).fetchone()
        return row is not None

    def counts(self) -> dict[str, dict[str, int]]:
        rows = self._conn().execute(
            "SELECT kind, status, COUNT(*) c FROM jobs WHERE status IN ('queued','running') GROUP BY kind, status").fetchall()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["kind"], {})[r["status"]] = int(r["c"])
        return out

    def prune(self, keep_seconds: float = 3600.0, kinds: Iterable[str] | None = None) -> None:
        """Borra trabajos terminados y ya aplicados de hace más de `keep_seconds`."""
        cutoff = time.time() - keep_seconds
        self._tx(lambda c: c.execute(
            "DELETE FROM jobs WHERE status IN ('done','failed','cancelled') AND finished<? AND "
            "(seen>=2 OR kind IN ('render','stems'))", (cutoff,)))

    def heartbeat(self, worker: str, lanes: list[str], caps: dict[str, Any]) -> None:
        self._tx(lambda c: c.execute(
            "INSERT INTO workers(id,pid,host,lanes,caps,beat) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET beat=excluded.beat, caps=excluded.caps, lanes=excluded.lanes",
            (worker, os.getpid(), socket.gethostname(), json.dumps(lanes), json.dumps(caps), time.time())))

    def drop_worker(self, worker: str) -> None:
        self._tx(lambda c: c.execute("DELETE FROM workers WHERE id=?", (worker,)))

    def workers(self, max_age: float = 15.0) -> list[dict[str, Any]]:
        rows = self._conn().execute("SELECT * FROM workers WHERE beat>=?", (time.time() - max_age,)).fetchall()
        return [{"id": r["id"], "pid": r["pid"], "host": r["host"], "lanes": json.loads(r["lanes"] or "[]"),
                 "caps": json.loads(r["caps"] or "{}"), "age": round(time.time() - r["beat"], 1)} for r in rows]

    def capabilities(self, lane: str | None = None, max_age: float = 15.0) -> dict[str, Any]:
        """Lo que pueden hacer los workers vivos (unión de sus capacidades). Vacío si no hay ninguno."""
        merged: dict[str, Any] = {}
        for w in self.workers(max_age):
            if lane and lane not in w["lanes"]:
                continue
            for k, v in w["caps"].items():
                merged[k] = bool(merged.get(k)) or bool(v) if isinstance(v, bool) else v
        return merged

    def wait(self, job_id: int, timeout: float, poll: float = 0.1) -> Job:
        deadline = time.time() + timeout
        while True:
            j = self.get(job_id)
            if j is None:
                raise KeyError(job_id)
            if j.status in (DONE, FAILED, CANCELLED):
                return j
            if time.time() > deadline:
                return j
            time.sleep(poll)


def new_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:4]}"
