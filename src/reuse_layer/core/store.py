"""Local SQLite store: the HTTP cache and the outcome ledger.

The layer keeps no index of GitHub. It owns only what nobody else has:
which recommendations it made and what happened after integration.

Privacy: by default a recommendation stores a hash of the task, not its text,
and never any source code. Poisoning: an outcome must name a recommendation
this store issued and one of its picks, once per (recommendation, repo,
outcome).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    key TEXT PRIMARY KEY,
    status INTEGER NOT NULL,
    etag TEXT,
    body TEXT NOT NULL,
    link TEXT,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS recommendation (
    id TEXT PRIMARY KEY,
    task_hash TEXT NOT NULL,
    task_text TEXT,
    picks TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outcome (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id TEXT NOT NULL REFERENCES recommendation(id),
    repo TEXT NOT NULL,
    outcome TEXT NOT NULL,
    replaced_with TEXT,
    note TEXT,
    created_at REAL NOT NULL,
    UNIQUE (recommendation_id, repo, outcome)
);
CREATE INDEX IF NOT EXISTS outcome_repo ON outcome(repo);
CREATE INDEX IF NOT EXISTS outcome_replacement ON outcome(replaced_with);
"""

GOOD = ("kept", "tests_passed")
BAD = ("removed", "replaced", "tests_failed")

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_recommendation_id() -> str:
    """``rec_`` + a 26-character ULID (48-bit millisecond time + 80 random bits)."""
    value = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "rec_" + "".join(reversed(chars))


@dataclass(frozen=True)
class CachedResponse:
    status: int
    etag: str | None
    body: str
    link: str | None
    fetched_at: float


@dataclass(frozen=True)
class Preference:
    good: int
    bad: int

    @property
    def n(self) -> int:
        return self.good + self.bad

    @property
    def score(self) -> float:
        """Beta(1,1)-smoothed retention: 0.5 with no data."""
        return (self.good + 1) / (self.n + 2)


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if str(path) != ":memory:" else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        if self.path is not None and os.name == "posix":
            os.chmod(self.path, 0o600)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ cache

    def cache_get(self, key: str) -> CachedResponse | None:
        with self._lock:
            row = self._db.execute(
                "SELECT status, etag, body, link, fetched_at FROM http_cache WHERE key = ?", (key,)
            ).fetchone()
        return CachedResponse(*row) if row else None

    def cache_put(self, key: str, status: int, etag: str | None, body: str, link: str | None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO http_cache (key, status, etag, body, link, fetched_at) VALUES (?,?,?,?,?,?)",
                (key, status, etag, body, link, time.time()),
            )

    def cache_touch(self, key: str) -> None:
        with self._lock:
            self._db.execute("UPDATE http_cache SET fetched_at = ? WHERE key = ?", (time.time(), key))

    def cache_prune(self, older_than_s: float = 14 * 86400) -> int:
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM http_cache WHERE fetched_at < ?", (time.time() - older_than_s,)
            )
        return cur.rowcount

    # ------------------------------------------------------------------ ledger

    def save_recommendation(self, rec_id: str, task: str, picks: list[str], *, retain_text: bool) -> None:
        task_hash = hashlib.sha256(task.encode("utf-8")).hexdigest()
        with self._lock:
            self._db.execute(
                "INSERT INTO recommendation (id, task_hash, task_text, picks, created_at) VALUES (?,?,?,?,?)",
                (rec_id, task_hash, task if retain_text else None, json.dumps(picks), time.time()),
            )

    def recommendation_picks(self, rec_id: str) -> list[str] | None:
        with self._lock:
            row = self._db.execute("SELECT picks FROM recommendation WHERE id = ?", (rec_id,)).fetchone()
        return None if row is None else list(json.loads(row[0]))

    def record_outcome(
        self, rec_id: str, repo: str, outcome: str, replaced_with: str | None, note: str | None
    ) -> bool:
        """Insert one outcome; False if this exact report was already recorded."""
        try:
            with self._lock:
                self._db.execute(
                    "INSERT INTO outcome (recommendation_id, repo, outcome, replaced_with, note, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        rec_id,
                        repo.lower(),
                        outcome,
                        replaced_with.lower() if replaced_with else None,
                        note,
                        time.time(),
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def preference(self, repo: str) -> Preference:
        """Retention evidence for one repo: outcomes on it, plus migration votes for and against it."""
        r = repo.lower()
        with self._lock:
            rows = self._db.execute(
                "SELECT outcome, COUNT(*) FROM outcome WHERE repo = ? GROUP BY outcome", (r,)
            ).fetchall()
            wins = self._db.execute(
                "SELECT COUNT(*) FROM outcome WHERE replaced_with = ? AND outcome = 'replaced'", (r,)
            ).fetchone()[0]
        counts: dict[str, int] = {k: int(v) for k, v in rows}
        good = sum(counts.get(k, 0) for k in GOOD) + int(wins)
        bad = sum(counts.get(k, 0) for k in BAD)
        return Preference(good=good, bad=bad)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            recs = self._db.execute("SELECT COUNT(*) FROM recommendation").fetchone()[0]
            outs = self._db.execute("SELECT COUNT(*) FROM outcome").fetchone()[0]
            cache = self._db.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0]
        return {"recommendations": recs, "outcomes": outs, "cached_responses": cache}
