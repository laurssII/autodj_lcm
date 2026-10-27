"""Persistencia en SQLite: pistas, sesiones e historial de transiciones."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

from .models import Track

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    seed INTEGER NOT NULL,
    style TEXT NOT NULL,
    started_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    from_id TEXT,
    to_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    score REAL,
    requested INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transitions_pair ON transitions(from_id, to_id);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def upsert_track(self, t: Track) -> None:
        if not t.created_at:
            t.created_at = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO tracks(id, source, title, artist, status, created_at, data) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET source=excluded.source, title=excluded.title, "
                "artist=excluded.artist, status=excluded.status, data=excluded.data",
                (t.id, t.source, t.title, t.artist, t.status, t.created_at, json.dumps(t.to_dict())),
            )
            self._conn.commit()

    def get_track(self, tid: str) -> Track | None:
        with self._lock:
            row = self._conn.execute("SELECT data FROM tracks WHERE id=?", (tid,)).fetchone()
        return Track.from_dict(json.loads(row["data"])) if row else None

    def list_tracks(self) -> list[Track]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM tracks ORDER BY created_at").fetchall()
        return [Track.from_dict(json.loads(r["data"])) for r in rows]

    def delete_track(self, tid: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM tracks WHERE id=?", (tid,))
            self._conn.commit()

    def count_tracks(self, sources: Iterable[str] | None = None) -> int:
        with self._lock:
            if sources:
                src = list(sources)
                q = f"SELECT COUNT(*) FROM tracks WHERE source IN ({','.join('?' * len(src))})"
                return int(self._conn.execute(q, src).fetchone()[0])
            return int(self._conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])

    def add_session(self, sid: str, seed: int, style: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO sessions VALUES (?,?,?,?)", (sid, seed, style, time.time()))
            self._conn.commit()

    def add_transition(self, session_id: str, from_id: str | None, to_id: str, strategy: str,
                       score: float | None, requested: bool, reason: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO transitions(session_id, from_id, to_id, strategy, score, requested, reason, ts) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (session_id, from_id, to_id, strategy, score, int(requested), reason, time.time()),
            )
            self._conn.commit()

    def pair_counts(self, from_id: str | None = None) -> dict[tuple[str, str], int]:
        with self._lock:
            if from_id:
                rows = self._conn.execute(
                    "SELECT from_id, to_id, COUNT(*) c FROM transitions WHERE from_id=? GROUP BY from_id, to_id",
                    (from_id,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT from_id, to_id, COUNT(*) c FROM transitions WHERE from_id IS NOT NULL "
                    "GROUP BY from_id, to_id").fetchall()
        return {(r["from_id"], r["to_id"]): int(r["c"]) for r in rows}

    def openers(self) -> dict[str, int]:
        """Cuántas veces cada pista abrió una sesión (para variar el arranque)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT to_id, COUNT(*) c FROM transitions WHERE from_id IS NULL GROUP BY to_id").fetchall()
        return {r["to_id"]: int(r["c"]) for r in rows}
