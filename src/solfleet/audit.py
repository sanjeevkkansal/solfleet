"""Audit log. SQLite from day one so a future team/hosted mode shares
state without a redesign. Every mutation (dry-run or execute) is recorded."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

DEFAULT_DB = "solfleet.sqlite"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditLog:
    def __init__(self, path: str | Path = DEFAULT_DB, *, clock: Callable[[], str] | None = None):
        self.path = str(path)
        self._clock = clock or _utc_now_iso
        self._init()

    def _init(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts        TEXT    NOT NULL,
                    operation TEXT    NOT NULL,
                    cluster   TEXT,
                    node      TEXT,
                    mode      TEXT    NOT NULL,
                    allowed   INTEGER NOT NULL,
                    detail    TEXT
                )
                """
            )

    def record(
        self,
        *,
        operation: str,
        cluster: str,
        node: str,
        mode: str,
        allowed: bool,
        detail: dict | None = None,
    ) -> int:
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                "INSERT INTO events (ts, operation, cluster, node, mode, allowed, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self._clock(),
                    operation,
                    cluster,
                    node,
                    mode,
                    int(allowed),
                    json.dumps(detail) if detail is not None else None,
                ),
            )
            return int(cur.lastrowid)

    def recent(self, *, node: str | None = None, limit: int = 20) -> list[dict]:
        query = "SELECT ts, operation, cluster, node, mode, allowed, detail FROM events"
        params: list = []
        if node:
            query += " WHERE node = ?"
            params.append(node)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "ts": r[0],
                "operation": r[1],
                "cluster": r[2],
                "node": r[3],
                "mode": r[4],
                "allowed": bool(r[5]),
                "detail": json.loads(r[6]) if r[6] else None,
            }
            for r in rows
        ]
