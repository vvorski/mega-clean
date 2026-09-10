"""SQLite index: the single source of truth and the work queue.

Every command is a resumable pass over this table. The pending set is derived
from rows lacking both a signature and an error, so any interrupted run resumes
where it stopped.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    scope      TEXT NOT NULL,
    path       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    mtime      TEXT,
    sig        TEXT,
    exif_key   TEXT,
    phash      TEXT,
    phash_src  TEXT,
    width      INTEGER,
    height     INTEGER,
    error      TEXT,
    PRIMARY KEY (scope, path)
);
CREATE INDEX IF NOT EXISTS idx_nodes_scope_size ON nodes (scope, size);
CREATE INDEX IF NOT EXISTS idx_nodes_sig ON nodes (sig);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def open_index(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def upsert_node(conn: sqlite3.Connection, scope: str, path: str, size: int,
                mtime: str) -> None:
    """Record a node. A changed size invalidates any cached signature."""
    conn.execute(
        """
        INSERT INTO nodes (scope, path, size, mtime) VALUES (?, ?, ?, ?)
        ON CONFLICT (scope, path) DO UPDATE SET
            mtime = excluded.mtime,
            sig       = CASE WHEN nodes.size = excluded.size THEN nodes.sig END,
            exif_key  = CASE WHEN nodes.size = excluded.size THEN nodes.exif_key END,
            phash     = CASE WHEN nodes.size = excluded.size THEN nodes.phash END,
            phash_src = CASE WHEN nodes.size = excluded.size THEN nodes.phash_src END,
            width     = CASE WHEN nodes.size = excluded.size THEN nodes.width END,
            height    = CASE WHEN nodes.size = excluded.size THEN nodes.height END,
            error     = CASE WHEN nodes.size = excluded.size THEN nodes.error END,
            size      = excluded.size
        """,
        (scope, path, size, mtime),
    )
    conn.commit()


def set_signature(conn: sqlite3.Connection, scope: str, path: str, *,
                  sig: str | None, exif_key: str | None, phash: str | None,
                  phash_src: str | None, width: int | None,
                  height: int | None) -> None:
    conn.execute(
        """
        UPDATE nodes SET sig = ?, exif_key = ?, phash = ?, phash_src = ?,
                         width = ?, height = ?, error = NULL
        WHERE scope = ? AND path = ?
        """,
        (sig, exif_key, phash, phash_src, width, height, scope, path),
    )
    conn.commit()


def set_error(conn: sqlite3.Connection, scope: str, path: str,
              error: str) -> None:
    conn.execute(
        "UPDATE nodes SET error = ? WHERE scope = ? AND path = ?",
        (error, scope, path),
    )
    conn.commit()


def iter_nodes(conn: sqlite3.Connection, scope: str) -> Iterator[sqlite3.Row]:
    yield from conn.execute(
        "SELECT * FROM nodes WHERE scope = ? ORDER BY path", (scope,)
    )


def size_collision_paths(conn: sqlite3.Connection, scope: str) -> list[str]:
    """Paths whose byte size is shared with at least one other node.

    Files with a unique size cannot be byte-identical to anything, so this is
    the whole candidate set for exact-duplicate detection.
    """
    rows = conn.execute(
        """
        SELECT path FROM nodes WHERE scope = ? AND size IN (
            SELECT size FROM nodes WHERE scope = ?
            GROUP BY size HAVING COUNT(*) > 1
        )
        """,
        (scope, scope),
    )
    return [r["path"] for r in rows]


def pending_paths(conn: sqlite3.Connection, scope: str,
                  paths: list[str] | None = None) -> list[str]:
    """Nodes still needing a signature: no sig recorded and no error logged."""
    rows = conn.execute(
        "SELECT path FROM nodes WHERE scope = ? AND sig IS NULL "
        "AND error IS NULL ORDER BY path",
        (scope,),
    )
    pending = [r["path"] for r in rows]
    if paths is None:
        return pending
    wanted = set(paths)
    return [p for p in pending if p in wanted]


def clear_errors(conn: sqlite3.Connection, scope: str) -> int:
    cur = conn.execute(
        "UPDATE nodes SET error = NULL WHERE scope = ? AND error IS NOT NULL",
        (scope,),
    )
    conn.commit()
    return cur.rowcount
