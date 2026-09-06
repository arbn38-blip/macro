"""SQLite persistence. Three tables: time series, JSON docs, fetcher health."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS series_points(
  series_id TEXT NOT NULL,
  d         TEXT NOT NULL,
  value     REAL NOT NULL,
  PRIMARY KEY(series_id, d)
);
CREATE TABLE IF NOT EXISTS docs(
  key        TEXT PRIMARY KEY,
  payload    TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  source     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fetcher_status(
  name          TEXT PRIMARY KEY,
  last_success  TEXT,
  last_error    TEXT,
  last_error_at TEXT,
  active_source TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Doc:
    payload: Any
    updated_at: str
    source: str


class Store:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self.conn.executescript(SCHEMA)

    def upsert_points(self, series_id: str, points: Iterable[tuple[date, float]]) -> None:
        with self._lock:
            self.conn.executemany(
                "INSERT INTO series_points(series_id, d, value) VALUES(?,?,?) "
                "ON CONFLICT(series_id, d) DO UPDATE SET value=excluded.value",
                [(series_id, dt.isoformat(), v) for dt, v in points],
            )
            self.conn.commit()

    def points(self, series_id: str, since: date | None = None) -> dict[date, float]:
        q = "SELECT d, value FROM series_points WHERE series_id=?"
        args: list[Any] = [series_id]
        if since is not None:
            q += " AND d >= ?"
            args.append(since.isoformat())
        with self._lock:
            rows = self.conn.execute(q, args).fetchall()
        return {
            date.fromisoformat(d): v
            for d, v in rows
            if isinstance(v, (int, float))  # sqlite dynamic typing: skip corrupt rows
        }

    def put_doc(self, key: str, payload: Any, source: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO docs(key, payload, updated_at, source) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
                "updated_at=excluded.updated_at, source=excluded.source",
                (key, json.dumps(payload), _now(), source),
            )
            self.conn.commit()

    def doc(self, key: str) -> Doc | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT payload, updated_at, source FROM docs WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            # a corrupted doc must behave like a missing doc, never 500 the API
            log.warning("dropping corrupted doc %r", key)
            return None
        return Doc(payload=payload, updated_at=row[1], source=row[2])

    def record_success(self, name: str, active_source: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO fetcher_status(name, last_success, active_source) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET last_success=excluded.last_success, "
                "active_source=excluded.active_source",
                (name, _now(), active_source),
            )
            self.conn.commit()

    def record_error(self, name: str, error: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO fetcher_status(name, last_error, last_error_at) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET last_error=excluded.last_error, "
                "last_error_at=excluded.last_error_at",
                (name, error, _now()),
            )
            self.conn.commit()

    def prune_outside_range(self, series_id: str, lo: float, hi: float) -> None:
        """Delete stored points outside [lo, hi] — cleanup for feeds that once
        served corrupt values (upsert alone never removes them)."""
        with self._lock:
            self.conn.execute(
                "DELETE FROM series_points WHERE series_id=? AND (value < ? OR value > ?)",
                (series_id, lo, hi),
            )
            self.conn.commit()

    def status(self, name: str) -> dict[str, Any] | None:
        return next((s for s in self.statuses() if s["name"] == name), None)

    def statuses(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT name, last_success, last_error, last_error_at, active_source "
                "FROM fetcher_status ORDER BY name"
            ).fetchall()
        cols = ["name", "last_success", "last_error", "last_error_at", "active_source"]
        return [dict(zip(cols, r)) for r in rows]
