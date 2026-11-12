"""SQLite persistence, version state machine and crash recovery.

Layout
------
Two catalog tables hold document/version metadata. Every version of a document
gets its own set of tables (``blocks_<vid>``, ``unigrams_<vid>``,
``bigrams_<vid>``) so that a replacement can be streamed, indexed and only
switched in once complete, and dropping an obsolete version never produces a
giant WAL inside a shared table.

A version moves through the states

    INGESTING -> INDEXING -> READY
                   \\------> FAILED

Only ``READY`` versions are searchable. Crash recovery on startup turns every
non-terminal version into ``FAILED`` and removes its staging tables, so an
interrupted replacement never masquerades as a usable index.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from . import config

STATE_INGESTING = "ingesting"
STATE_INDEXING = "indexing"
STATE_READY = "ready"
STATE_FAILED = "failed"
TERMINAL_STATES = (STATE_READY, STATE_FAILED)


def get_conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open a connection with sane durability/concurrency settings."""
    path = db_path or str(config.DB_PATH)
    # Connections are request-private but may be driven from a worker thread
    # (streaming ingestion runs in a threadpool), so disable the thread check.
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            doc_id            TEXT PRIMARY KEY,
            active_version_id INTEGER,
            created_at        REAL NOT NULL,
            updated_at        REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS versions (
            version_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id       TEXT NOT NULL REFERENCES documents(doc_id),
            state        TEXT NOT NULL,
            created_at   REAL NOT NULL,
            finalized_at REAL,
            error        TEXT,
            block_count  INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_versions_doc ON versions(doc_id, state);
        """
    )
    conn.commit()


def _version_tables(vid: int) -> tuple[str, str, str]:
    return f"blocks_{vid}", f"unigrams_{vid}", f"bigrams_{vid}"


def create_version_tables(conn: sqlite3.Connection, vid: int) -> None:
    blocks, unigrams, bigrams = _version_tables(vid)
    conn.executescript(
        f"""
        CREATE TABLE {blocks} (
            block_order INTEGER PRIMARY KEY,
            block_id    TEXT NOT NULL UNIQUE,
            page        INTEGER NOT NULL,
            text        TEXT NOT NULL,
            x0          REAL,
            y0          REAL,
            x1          REAL,
            y1          REAL,
            start_pos   INTEGER NOT NULL,
            end_pos     INTEGER NOT NULL,
            nws_len     INTEGER NOT NULL
        );
        CREATE INDEX idx_{blocks}_start ON {blocks}(start_pos);
        CREATE TABLE {unigrams} (
            ch  TEXT NOT NULL,
            pos INTEGER NOT NULL,
            PRIMARY KEY (ch, pos)
        ) WITHOUT ROWID;
        CREATE TABLE {bigrams} (
            bg  TEXT NOT NULL,
            pos INTEGER NOT NULL,
            PRIMARY KEY (bg, pos)
        ) WITHOUT ROWID;
        """
    )


def drop_version_tables(conn: sqlite3.Connection, vid: int) -> None:
    blocks, unigrams, bigrams = _version_tables(vid)
    conn.execute(f"DROP TABLE IF EXISTS {bigrams}")
    conn.execute(f"DROP TABLE IF EXISTS {unigrams}")
    conn.execute(f"DROP TABLE IF EXISTS {blocks}")


def has_pending_version(conn: sqlite3.Connection, doc_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM versions WHERE doc_id = ? AND state NOT IN (?, ?) LIMIT 1",
        (doc_id, STATE_READY, STATE_FAILED),
    ).fetchone()
    return row is not None


def create_version(conn: sqlite3.Connection, doc_id: str) -> int:
    """Register a new replacement version and its staging tables.

    Raises ``RuntimeError`` when another replacement for the same document is
    already in progress (enforced in the database, not only in process locks).
    """
    now = time.time()
    if has_pending_version(conn, doc_id):
        raise RuntimeError(f"document {doc_id!r} already has a replacement in progress")
    conn.execute(
        "INSERT INTO documents(doc_id, created_at, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(doc_id) DO UPDATE SET updated_at = excluded.updated_at",
        (doc_id, now, now),
    )
    cur = conn.execute(
        "INSERT INTO versions(doc_id, state, created_at) VALUES(?, ?, ?)",
        (doc_id, STATE_INGESTING, now),
    )
    vid = int(cur.lastrowid)
    create_version_tables(conn, vid)
    conn.commit()
    return vid


def set_state(
    conn: sqlite3.Connection,
    vid: int,
    state: str,
    error: Optional[str] = None,
    block_count: Optional[int] = None,
) -> None:
    if block_count is None:
        conn.execute(
            "UPDATE versions SET state = ?, error = COALESCE(?, error) WHERE version_id = ?",
            (state, error, vid),
        )
    else:
        conn.execute(
            "UPDATE versions SET state = ?, error = COALESCE(?, error), "
            "block_count = ?, finalized_at = ? WHERE version_id = ?",
            (state, error, block_count, time.time(), vid),
        )
    conn.commit()


def fail_version(conn: sqlite3.Connection, vid: int, error: str) -> None:
    """Mark a version failed and delete its staging data in one transaction."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE versions SET state = ?, error = ?, finalized_at = ? WHERE version_id = ?",
            (STATE_FAILED, error[:2000], time.time(), vid),
        )
        drop_version_tables(conn, vid)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def activate_version(
    conn: sqlite3.Connection, doc_id: str, vid: int, block_count: int
) -> None:
    """Atomically point the document at the newly built version.

    The catalog flip happens in one transaction; the old version's tables are
    dropped afterwards in a second transaction so readers always see either the
    complete old version or the complete new one.
    """
    old = conn.execute(
        "SELECT active_version_id FROM documents WHERE doc_id = ?", (doc_id,)
    ).fetchone()
    old_vid = int(old["active_version_id"]) if old and old["active_version_id"] else None
    with transaction(conn):
        conn.execute(
            "UPDATE versions SET state = ?, finalized_at = ?, block_count = ? "
            "WHERE version_id = ?",
            (STATE_READY, time.time(), block_count, vid),
        )
        conn.execute(
            "UPDATE documents SET active_version_id = ?, updated_at = ? WHERE doc_id = ?",
            (vid, time.time(), doc_id),
        )
    if old_vid is not None and old_vid != vid:
        with transaction(conn):
            drop_version_tables(conn, old_vid)


def get_active_version(conn: sqlite3.Connection, doc_id: str) -> Optional[int]:
    row = conn.execute(
        "SELECT active_version_id FROM documents WHERE doc_id = ?", (doc_id,)
    ).fetchone()
    if row is None or row["active_version_id"] is None:
        return None
    return int(row["active_version_id"])


def get_version(conn: sqlite3.Connection, vid: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM versions WHERE version_id = ?", (vid,)
    ).fetchone()


def list_documents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT d.doc_id, d.active_version_id, d.updated_at, "
            "v.block_count, v.state AS active_state "
            "FROM documents d LEFT JOIN versions v ON v.version_id = d.active_version_id "
            "ORDER BY d.doc_id"
        )
    )


def startup_recovery(conn: sqlite3.Connection) -> list[str]:
    """Recover from interrupted processes.

    Every version that never reached a terminal state is marked FAILED and its
    staging tables are removed. Returns human-readable recovery messages.
    """
    messages: list[str] = []
    init_schema(conn)
    stuck = conn.execute(
        "SELECT version_id, doc_id, state FROM versions "
        "WHERE state NOT IN (?, ?)",
        TERMINAL_STATES,
    ).fetchall()
    for row in stuck:
        vid = int(row["version_id"])
        fail_version(conn, vid, "interrupted before completion; marked failed on restart")
        messages.append(
            f"version {vid} of document {row['doc_id']!r} marked FAILED "
            f"(was {row['state']!r})"
        )

    # Garbage-collect tables belonging to failed versions (e.g. tables that
    # survived because fail_version itself was interrupted mid-way).
    existing = {
        name
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    failed_ids = [
        int(r["version_id"])
        for r in conn.execute(
            "SELECT version_id FROM versions WHERE state = ?", (STATE_FAILED,)
        ).fetchall()
    ]
    for vid in failed_ids:
        blocks, unigrams, bigrams = _version_tables(vid)
        with transaction(conn):
            for name in (bigrams, unigrams, blocks):
                if name in existing:
                    conn.execute(f"DROP TABLE {name}")
    return messages
