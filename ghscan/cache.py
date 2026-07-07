# sqlite state, one file per run
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS org_repos (
    full_name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    archived INTEGER NOT NULL,
    fork INTEGER NOT NULL,
    pushed_at TEXT,
    created_at TEXT,
    language TEXT,
    stargazers_count INTEGER NOT NULL DEFAULT 0,
    size_kb INTEGER NOT NULL DEFAULT 0,
    contributors_fetched INTEGER NOT NULL DEFAULT 0,
    discovered_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS contributors (
    login TEXT PRIMARY KEY,
    repos_fetched INTEGER NOT NULL DEFAULT 0,
    discovered_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS contributor_sightings (
    login TEXT NOT NULL,
    source_repo TEXT NOT NULL,
    PRIMARY KEY (login, source_repo)
);

CREATE TABLE IF NOT EXISTS contributor_repos (
    full_name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    archived INTEGER NOT NULL,
    fork INTEGER NOT NULL,
    pushed_at TEXT,
    created_at TEXT,
    language TEXT,
    stargazers_count INTEGER NOT NULL DEFAULT 0,
    size_kb INTEGER NOT NULL DEFAULT 0,
    discovered_via TEXT NOT NULL,
    discovered_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_queue (
    full_name TEXT PRIMARY KEY,
    clone_url TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    skip_reason TEXT,
    queued_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_results (
    full_name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    exit_code INTEGER,
    duration_seconds REAL,
    findings_count INTEGER NOT NULL DEFAULT 0,
    findings_json TEXT,
    stdout TEXT,
    stderr TEXT,
    error TEXT,
    started_at REAL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS fork_ahead_status (
    full_name TEXT PRIMARY KEY,
    ahead_by INTEGER NOT NULL,
    checked_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Cache:
    _schema_version = 1

    def __init__(self, db_path: Path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        # backfills cols for old dbs
        migrations = [
            ("org_repos", "pushed_at", "TEXT"),
            ("org_repos", "stargazers_count", "INTEGER NOT NULL DEFAULT 0"),
            ("org_repos", "size_kb", "INTEGER NOT NULL DEFAULT 0"),
            ("org_repos", "created_at", "TEXT"),
            ("org_repos", "language", "TEXT"),
            ("contributor_repos", "pushed_at", "TEXT"),
            ("contributor_repos", "stargazers_count", "INTEGER NOT NULL DEFAULT 0"),
            ("contributor_repos", "size_kb", "INTEGER NOT NULL DEFAULT 0"),
            ("contributor_repos", "created_at", "TEXT"),
            ("contributor_repos", "language", "TEXT"),
        ]
        cur = self._conn.cursor()
        for table, column, coltype in migrations:
            existing = {row["name"] for row in cur.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        cur.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _cursor(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def set_meta(self, key: str, value: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._cursor() as cur:
            row = cur.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def add_org_repo(self, repo: dict) -> bool:
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO org_repos"
                "(full_name, owner, archived, fork, pushed_at, created_at, language, "
                "stargazers_count, size_kb, discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    repo["full_name"], repo["owner"]["login"],
                    int(repo.get("archived", False)), int(repo.get("fork", False)),
                    repo.get("pushed_at"), repo.get("created_at"), repo.get("language"),
                    int(repo.get("stargazers_count", 0) or 0),
                    int(repo.get("size", 0) or 0),
                    time.time(),
                ),
            )
            return cur.rowcount > 0

    def org_repos_pending_contributors(self) -> list:
        with self._cursor() as cur:
            return cur.execute(
                "SELECT * FROM org_repos WHERE contributors_fetched=0"
            ).fetchall()

    def mark_org_repo_contributors_fetched(self, full_name: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE org_repos SET contributors_fetched=1 WHERE full_name=?", (full_name,)
            )

    def all_org_repos(self) -> list:
        with self._cursor() as cur:
            return cur.execute("SELECT * FROM org_repos").fetchall()

    def add_contributor_sighting(self, login: str, source_repo: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO contributor_sightings(login, source_repo) VALUES (?, ?)",
                (login, source_repo),
            )
            cur.execute(
                "INSERT OR IGNORE INTO contributors(login, discovered_at) VALUES (?, ?)",
                (login, time.time()),
            )
            return cur.rowcount > 0

    def contributors_pending_repos(self) -> list:
        with self._cursor() as cur:
            return cur.execute("SELECT * FROM contributors WHERE repos_fetched=0").fetchall()

    def mark_contributor_repos_fetched(self, login: str) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE contributors SET repos_fetched=1 WHERE login=?", (login,))

    def count_contributor_sightings(self) -> int:
        with self._cursor() as cur:
            return cur.execute("SELECT COUNT(*) c FROM contributor_sightings").fetchone()["c"]

    def count_unique_contributors(self) -> int:
        with self._cursor() as cur:
            return cur.execute("SELECT COUNT(*) c FROM contributors").fetchone()["c"]

    def add_org_member(self, login: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO contributors(login, discovered_at) VALUES (?, ?)",
                (login, time.time()),
            )
            return cur.rowcount > 0

    def add_contributor_repo(self, repo: dict, discovered_via: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO contributor_repos"
                "(full_name, owner, archived, fork, pushed_at, created_at, language, "
                "stargazers_count, size_kb, discovered_via, discovered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    repo["full_name"], repo["owner"]["login"],
                    int(repo.get("archived", False)), int(repo.get("fork", False)),
                    repo.get("pushed_at"), repo.get("created_at"), repo.get("language"),
                    int(repo.get("stargazers_count", 0) or 0),
                    int(repo.get("size", 0) or 0),
                    discovered_via, time.time(),
                ),
            )
            return cur.rowcount > 0

    def count_contributor_repos_discovered(self) -> int:
        with self._cursor() as cur:
            return cur.execute("SELECT COUNT(*) c FROM contributor_repos").fetchone()["c"]

    def all_contributor_repos(self) -> list:
        with self._cursor() as cur:
            return cur.execute("SELECT * FROM contributor_repos").fetchall()

    def get_repo_pushed_at(self, full_name: str, source: str) -> Optional[str]:
        # last known push time for incremental rescan
        table = "org_repos" if source == "org" else "contributor_repos"
        with self._cursor() as cur:
            row = cur.execute(
                f"SELECT pushed_at FROM {table} WHERE full_name=?", (full_name,)
            ).fetchone()
            return row["pushed_at"] if row else None

    def get_fork_ahead(self, full_name: str) -> Optional[int]:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT ahead_by FROM fork_ahead_status WHERE full_name=?", (full_name,)
            ).fetchone()
            return row["ahead_by"] if row else None

    def set_fork_ahead(self, full_name: str, ahead_by: int) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO fork_ahead_status(full_name, ahead_by, checked_at) VALUES (?, ?, ?) "
                "ON CONFLICT(full_name) DO UPDATE SET ahead_by=excluded.ahead_by, "
                "checked_at=excluded.checked_at",
                (full_name, ahead_by, time.time()),
            )

    def enqueue(self, full_name: str, clone_url: str, source: str,
                skip_reason: Optional[str] = None, queued_at: Optional[float] = None) -> bool:
        status = "skipped" if skip_reason else "pending"
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO scan_queue"
                "(full_name, clone_url, source, status, skip_reason, queued_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (full_name, clone_url, source, status, skip_reason,
                 queued_at if queued_at is not None else time.time()),
            )
            return cur.rowcount > 0

    def reset_stuck_scans(self) -> int:
        # crash recovery
        with self._cursor() as cur:
            cur.execute("UPDATE scan_queue SET status='pending' WHERE status='in_progress'")
            return cur.rowcount

    def pending_scans(self) -> list:
        with self._cursor() as cur:
            return cur.execute(
                "SELECT * FROM scan_queue WHERE status='pending' ORDER BY queued_at"
            ).fetchall()

    def all_queue_entries(self) -> list:
        with self._cursor() as cur:
            return cur.execute("SELECT * FROM scan_queue").fetchall()

    def mark_scan_status(self, full_name: str, status: str) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE scan_queue SET status=? WHERE full_name=?", (status, full_name))

    def save_scan_result(self, result: dict) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO scan_results"
                "(full_name, status, exit_code, duration_seconds, findings_count, "
                "findings_json, stdout, stderr, error, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(full_name) DO UPDATE SET "
                "status=excluded.status, exit_code=excluded.exit_code, "
                "duration_seconds=excluded.duration_seconds, "
                "findings_count=excluded.findings_count, findings_json=excluded.findings_json, "
                "stdout=excluded.stdout, stderr=excluded.stderr, error=excluded.error, "
                "started_at=excluded.started_at, finished_at=excluded.finished_at",
                (
                    result["full_name"], result["status"], result.get("exit_code"),
                    result.get("duration_seconds"), result.get("findings_count", 0),
                    json.dumps(result.get("findings", [])), result.get("stdout"),
                    result.get("stderr"), result.get("error"), result.get("started_at"),
                    result.get("finished_at"),
                ),
            )

    def all_scan_results(self) -> list:
        with self._cursor() as cur:
            return cur.execute("SELECT * FROM scan_results").fetchall()

    def get_scan_result(self, full_name: str):
        with self._cursor() as cur:
            return cur.execute(
                "SELECT * FROM scan_results WHERE full_name=?", (full_name,)
            ).fetchone()

    def delete_scan_result(self, full_name: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM scan_results WHERE full_name=?", (full_name,))
