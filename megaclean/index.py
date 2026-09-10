"""SQLite index: the single source of truth and the work queue.

Every command is a resumable pass over this table. The pending set is derived
from rows lacking both a signature and an error, so any interrupted run resumes
where it stopped.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

SCHEMA_VERSION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    scope      TEXT NOT NULL,
    node_id    TEXT NOT NULL,
    path       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    mtime      TEXT,
    sig        TEXT,
    exif_key   TEXT,
    phash      TEXT,
    phash_src  TEXT,
    width      INTEGER,
    height     INTEGER,
    crc        TEXT,
    parent_id  TEXT,
    error      TEXT,
    -- Keyed on node_id, never on path: MEGA permits two files with the same
    -- name in the same folder, and those pairs are usually the duplicates we
    -- are looking for. Keying on path silently discards one of each.
    PRIMARY KEY (scope, node_id)
);
CREATE INDEX IF NOT EXISTS idx_nodes_scope_size ON nodes (scope, size);
CREATE INDEX IF NOT EXISTS idx_nodes_sig ON nodes (sig);
CREATE INDEX IF NOT EXISTS idx_nodes_crc ON nodes (crc);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def open_index(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an older index up to the current shape."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
    if not have:
        return
    for column in ("crc", "parent_id"):
        if column not in have:
            conn.execute(f"ALTER TABLE nodes ADD COLUMN {column} TEXT")
    if "node_id" not in have:
        # Pre-v3 rows were keyed on path; carry the path forward as the id.
        conn.execute("ALTER TABLE nodes ADD COLUMN node_id TEXT")
        conn.execute("UPDATE nodes SET node_id = path WHERE node_id IS NULL")
    conn.commit()


def set_parent(conn: sqlite3.Connection, scope: str, node_id: str,
               parent_id: str | None) -> None:
    conn.execute("UPDATE nodes SET parent_id = ? WHERE scope = ? AND node_id = ?",
                 (parent_id, scope, node_id))


def set_crc(conn: sqlite3.Connection, scope: str, node_id: str,
            crc: str | None) -> None:
    conn.execute("UPDATE nodes SET crc = ? WHERE scope = ? AND node_id = ?",
                 (crc, scope, node_id))


def crc_group_ids(conn: sqlite3.Connection, scope: str) -> list[str]:
    """Nodes whose content fingerprint is shared with at least one other file."""
    rows = conn.execute(
        """
        SELECT node_id FROM nodes WHERE scope = ? AND crc IS NOT NULL AND crc IN (
            SELECT crc FROM nodes WHERE scope = ? AND crc IS NOT NULL
            GROUP BY crc HAVING COUNT(*) > 1
        ) ORDER BY path
        """, (scope, scope))
    return [r["node_id"] for r in rows]


def upsert_node(conn: sqlite3.Connection, scope: str, path: str, size: int,
                mtime: str, node_id: str | None = None) -> None:
    """Record a node. A changed size invalidates any cached signature.

    `node_id` identifies the node; it defaults to the path for sources that
    have no stable id of their own (local files, rclone listings).
    """
    node_id = node_id or path
    conn.execute(
        """
        INSERT INTO nodes (scope, node_id, path, size, mtime)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (scope, node_id) DO UPDATE SET
            mtime = excluded.mtime,
            path  = excluded.path,
            sig       = CASE WHEN nodes.size = excluded.size THEN nodes.sig END,
            exif_key  = CASE WHEN nodes.size = excluded.size THEN nodes.exif_key END,
            phash     = CASE WHEN nodes.size = excluded.size THEN nodes.phash END,
            phash_src = CASE WHEN nodes.size = excluded.size THEN nodes.phash_src END,
            width     = CASE WHEN nodes.size = excluded.size THEN nodes.width END,
            height    = CASE WHEN nodes.size = excluded.size THEN nodes.height END,
            crc       = CASE WHEN nodes.size = excluded.size THEN nodes.crc END,
            error     = CASE WHEN nodes.size = excluded.size THEN nodes.error END,
            size      = excluded.size
        """,
        (scope, node_id, path, size, mtime),
    )
    conn.commit()


def set_signature(conn: sqlite3.Connection, scope: str, node_id: str, *,
                  sig: str | None, exif_key: str | None, phash: str | None,
                  phash_src: str | None, width: int | None,
                  height: int | None) -> None:
    conn.execute(
        """
        UPDATE nodes SET sig = ?, exif_key = ?, phash = ?, phash_src = ?,
                         width = ?, height = ?, error = NULL
        WHERE scope = ? AND node_id = ?
        """,
        (sig, exif_key, phash, phash_src, width, height, scope, node_id),
    )
    conn.commit()


def set_error(conn: sqlite3.Connection, scope: str, node_id: str,
              error: str) -> None:
    conn.execute(
        "UPDATE nodes SET error = ? WHERE scope = ? AND node_id = ?",
        (error, scope, node_id),
    )
    conn.commit()


def iter_nodes(conn: sqlite3.Connection, scope: str) -> Iterator[sqlite3.Row]:
    yield from conn.execute(
        "SELECT * FROM nodes WHERE scope = ? ORDER BY path", (scope,)
    )


def size_collision_ids(conn: sqlite3.Connection, scope: str) -> list[str]:
    """Paths whose byte size is shared with at least one other node.

    Files with a unique size cannot be byte-identical to anything, so this is
    the whole candidate set for exact-duplicate detection.
    """
    rows = conn.execute(
        """
        SELECT node_id FROM nodes WHERE scope = ? AND size IN (
            SELECT size FROM nodes WHERE scope = ?
            GROUP BY size HAVING COUNT(*) > 1
        )
        """,
        (scope, scope),
    )
    return [r["node_id"] for r in rows]


def pending_ids(conn: sqlite3.Connection, scope: str,
                node_ids: list[str] | None = None) -> list[str]:
    """Nodes still needing a signature: no sig recorded and no error logged."""
    rows = conn.execute(
        "SELECT node_id FROM nodes WHERE scope = ? AND sig IS NULL "
        "AND error IS NULL ORDER BY path",
        (scope,),
    )
    pending = [r["node_id"] for r in rows]
    if node_ids is None:
        return pending
    wanted = set(node_ids)
    return [n for n in pending if n in wanted]


def targets_for(conn: sqlite3.Connection, scope: str,
                node_ids: list[str]) -> list[tuple[str, str, int]]:
    """(node_id, path, size) for the given nodes, in path order."""
    wanted = set(node_ids)
    return [(r["node_id"], r["path"], r["size"])
            for r in iter_nodes(conn, scope) if r["node_id"] in wanted]


def clear_errors(conn: sqlite3.Connection, scope: str) -> int:
    cur = conn.execute(
        "UPDATE nodes SET error = NULL WHERE scope = ? AND error IS NOT NULL",
        (scope,),
    )
    conn.commit()
    return cur.rowcount
