# mega-clean Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python CLI that indexes a MEGA account's photos, reports duplicate and variant clusters without ever deleting anything, and uploads only the local photos whose content is not already in the account.

**Architecture:** A SQLite index is the single source of truth and the work queue; every command is a resumable pass over it. Content identity is derived client-side because MEGA exposes no hash: a cheap tier hashes head+tail bytes of size-colliding files only, and an opt-in tier reads the first 64 KB of every image for EXIF and embedded-thumbnail perceptual hashing. All network access sits behind a `Remote` protocol with an in-memory fake, so every flow is testable offline.

**Tech Stack:** Python 3.11+, stdlib `sqlite3` and `argparse`, `exifread`, `Pillow`, `pillow-heif`, `ImageHash`; external `rclone` (required), MEGAcmd (optional).

**Spec:** `docs/superpowers/specs/2026-09-10-mega-clean-design.md`

## Global Constraints

- Python 3.11+.
- Third-party dependencies limited to exactly: `exifread`, `Pillow`, `pillow-heif`, `ImageHash`, plus `pytest` for tests. CLI parsing uses stdlib `argparse`; no `click`.
- **No deletion code anywhere in the project.** No task may add a call that removes a remote or local file. `dupes` reports only; `upload` only creates.
- `HEAD_BYTES = 65536`, `TAIL_BYTES = 65536`, default perceptual-hash Hamming threshold `6`, default fingerprint thread pool `8`.
- Perceptual hashes carry a provenance of `"thumb"` or `"full"` and are only ever compared within the same provenance class.
- All subprocess invocation goes through an injectable runner so tests never shell out.
- Every command must be interruptible and re-runnable; the pending work set is derived from the index, never from in-memory state.
- Tests use fixture images generated at test time with Pillow. No binary fixtures are committed.

---

### Task 1: Project scaffolding and the SQLite index

**Files:**
- Create: `pyproject.toml`
- Create: `megaclean/__init__.py`
- Create: `megaclean/index.py`
- Test: `tests/test_index.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `SCHEMA_VERSION: int`
  - `open_index(path: str | Path) -> sqlite3.Connection`
  - `upsert_node(conn, scope: str, path: str, size: int, mtime: str) -> None`
  - `set_signature(conn, scope: str, path: str, *, sig: str | None, exif_key: str | None, phash: str | None, phash_src: str | None, width: int | None, height: int | None) -> None`
  - `set_error(conn, scope: str, path: str, error: str) -> None`
  - `iter_nodes(conn, scope: str) -> Iterator[sqlite3.Row]`
  - `size_collision_paths(conn, scope: str) -> list[str]`
  - `pending_paths(conn, scope: str, paths: list[str] | None = None) -> list[str]`
  - `clear_errors(conn, scope: str) -> int` — clears logged errors so `--retry` can re-queue them
  - Scope strings: `"remote"` for the account, `f"local:{abspath}"` for a local root.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_index.py
import pytest
from megaclean.index import (
    open_index, upsert_node, set_signature, set_error,
    iter_nodes, size_collision_paths, pending_paths,
)


@pytest.fixture
def conn(tmp_path):
    return open_index(tmp_path / "index.db")


def test_upsert_is_idempotent_and_updates_size(conn):
    upsert_node(conn, "remote", "/Photos/a.jpg", 100, "2024-01-01T00:00:00Z")
    upsert_node(conn, "remote", "/Photos/a.jpg", 120, "2024-01-02T00:00:00Z")
    rows = list(iter_nodes(conn, "remote"))
    assert len(rows) == 1
    assert rows[0]["size"] == 120
    assert rows[0]["mtime"] == "2024-01-02T00:00:00Z"


def test_scopes_are_independent(conn):
    upsert_node(conn, "remote", "/a.jpg", 1, "t")
    upsert_node(conn, "local:/tmp/pics", "/a.jpg", 1, "t")
    assert len(list(iter_nodes(conn, "remote"))) == 1
    assert len(list(iter_nodes(conn, "local:/tmp/pics"))) == 1


def test_size_collision_paths_ignores_unique_sizes(conn):
    upsert_node(conn, "remote", "/a.jpg", 100, "t")
    upsert_node(conn, "remote", "/b.jpg", 100, "t")
    upsert_node(conn, "remote", "/c.jpg", 999, "t")
    assert sorted(size_collision_paths(conn, "remote")) == ["/a.jpg", "/b.jpg"]


def test_pending_excludes_signed_and_errored(conn):
    for p in ("/a.jpg", "/b.jpg", "/c.jpg"):
        upsert_node(conn, "remote", p, 100, "t")
    set_signature(conn, "remote", "/a.jpg", sig="deadbeef", exif_key=None,
                  phash=None, phash_src=None, width=None, height=None)
    set_error(conn, "remote", "/b.jpg", "timeout")
    assert pending_paths(conn, "remote") == ["/c.jpg"]


def test_set_signature_clears_previous_error(conn):
    upsert_node(conn, "remote", "/a.jpg", 100, "t")
    set_error(conn, "remote", "/a.jpg", "timeout")
    set_signature(conn, "remote", "/a.jpg", sig="beef", exif_key=None,
                  phash=None, phash_src=None, width=None, height=None)
    row = next(iter_nodes(conn, "remote"))
    assert row["error"] is None
    assert row["sig"] == "beef"


def test_upsert_preserves_signature_when_size_unchanged(conn):
    upsert_node(conn, "remote", "/a.jpg", 100, "t")
    set_signature(conn, "remote", "/a.jpg", sig="beef", exif_key="k",
                  phash="ff", phash_src="thumb", width=10, height=20)
    upsert_node(conn, "remote", "/a.jpg", 100, "t2")
    assert next(iter_nodes(conn, "remote"))["sig"] == "beef"


def test_upsert_invalidates_signature_when_size_changes(conn):
    upsert_node(conn, "remote", "/a.jpg", 100, "t")
    set_signature(conn, "remote", "/a.jpg", sig="beef", exif_key="k",
                  phash="ff", phash_src="thumb", width=10, height=20)
    upsert_node(conn, "remote", "/a.jpg", 101, "t2")
    row = next(iter_nodes(conn, "remote"))
    assert row["sig"] is None
    assert row["exif_key"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_index.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'megaclean'`

- [ ] **Step 3: Write `pyproject.toml`**

```toml
[project]
name = "megaclean"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "exifread>=3.0",
    "Pillow>=10.0",
    "pillow-heif>=0.15",
    "ImageHash>=4.3",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[project.scripts]
megaclean = "megaclean.cli:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["integration: touches a real MEGA account; skipped by default"]
addopts = "-m 'not integration'"
```

- [ ] **Step 4: Write the implementation**

```python
# megaclean/__init__.py
"""Client-side content identity for MEGA photo libraries."""
__version__ = "0.1.0"
```

```python
# megaclean/index.py
"""SQLite index: the single source of truth and the work queue.

Every command is a resumable pass over this table. The pending set is derived
from rows lacking both a signature and an error, so any interrupted run simply
resumes where it stopped.
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pip install -e '.[dev]' && pytest tests/test_index.py -v`
Expected: all 6 tests PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml megaclean/ tests/
git commit -m "feat: SQLite index with resumable pending-work queue"
```

---

### Task 2: Content signature

**Files:**
- Create: `megaclean/signature.py`
- Test: `tests/test_signature.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `HEAD_BYTES: int = 65536`, `TAIL_BYTES: int = 65536`
  - `content_signature(size: int, head: bytes, tail: bytes) -> str` — hex digest.
  - `signature_of_bytes(data: bytes) -> str` — for a whole file already in memory; must agree with `content_signature` for the same file.
  - `needs_tail_read(size: int) -> bool` — False when head and tail would overlap.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_signature.py
import pytest
from megaclean.signature import (
    HEAD_BYTES, TAIL_BYTES, content_signature, signature_of_bytes,
    needs_tail_read,
)


def test_signature_is_stable_and_hex():
    sig = content_signature(1000, b"head", b"tail")
    assert sig == content_signature(1000, b"head", b"tail")
    assert len(sig) == 32
    int(sig, 16)


def test_size_participates_in_the_signature():
    assert content_signature(1000, b"h", b"t") != content_signature(1001, b"h", b"t")


def test_head_and_tail_are_not_interchangeable():
    assert content_signature(10, b"a", b"b") != content_signature(10, b"b", b"a")


def test_small_files_need_no_tail_read():
    assert needs_tail_read(HEAD_BYTES + TAIL_BYTES + 1) is True
    assert needs_tail_read(HEAD_BYTES + TAIL_BYTES) is False
    assert needs_tail_read(10) is False


def test_whole_file_signature_agrees_with_ranged_signature():
    data = bytes(range(256)) * 1024          # 262144 bytes, larger than head+tail
    ranged = content_signature(len(data), data[:HEAD_BYTES], data[-TAIL_BYTES:])
    assert signature_of_bytes(data) == ranged


def test_small_whole_file_agrees_with_overlapping_range():
    data = b"tiny file contents"
    ranged = content_signature(len(data), data, b"")
    assert signature_of_bytes(data) == ranged


def test_different_content_same_size_differs():
    a = b"A" * 200000
    b = b"A" * 199999 + b"B"
    assert signature_of_bytes(a) != signature_of_bytes(b)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_signature.py -v`
Expected: FAIL with `ImportError: cannot import name 'content_signature'`

- [ ] **Step 3: Write the implementation**

```python
# megaclean/signature.py
"""Client-side content identity.

MEGA exposes no content hash, so identity is derived from bytes we fetch
ourselves. The exact signature reads only the first and last 64 KB, which is
enough to separate photos that merely share a byte size.
"""
from __future__ import annotations

import hashlib

HEAD_BYTES = 65536
TAIL_BYTES = 65536


def needs_tail_read(size: int) -> bool:
    """True when a separate tail read would cover bytes the head read misses."""
    return size > HEAD_BYTES + TAIL_BYTES


def content_signature(size: int, head: bytes, tail: bytes) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update(size.to_bytes(8, "little"))
    h.update(len(head).to_bytes(4, "little"))
    h.update(head)
    h.update(tail)
    return h.hexdigest()


def signature_of_bytes(data: bytes) -> str:
    """Signature for a file whose full contents are already in memory."""
    size = len(data)
    if needs_tail_read(size):
        return content_signature(size, data[:HEAD_BYTES], data[-TAIL_BYTES:])
    return content_signature(size, data, b"")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_signature.py -v`
Expected: all 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add megaclean/signature.py tests/test_signature.py
git commit -m "feat: head+tail content signature"
```

---

### Task 3: EXIF key extraction from a truncated head buffer

**Files:**
- Modify: `megaclean/signature.py`
- Create: `tests/conftest.py`
- Test: `tests/test_exif.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `exif_key(head: bytes) -> str | None` — `"Make|Model|DateTimeOriginal|SubSec|ExposureTime|FNumber"`, or `None` when `DateTimeOriginal` is absent.
  - `embedded_thumbnail(head: bytes) -> bytes | None`
  - `conftest.py` fixture factory `make_jpeg(path, *, size=(64,48), color=(255,0,0), exif=None, quality=90) -> Path`

**Critical detail:** EXIF lives in the JPEG APP1 segment at the front of the file, so `exifread` parses it from a 64 KB head buffer without the rest of the file. Pass `details=True` so the embedded thumbnail is extracted too.

- [ ] **Step 1: Write the fixture factory**

```python
# tests/conftest.py
"""Fixture images are generated at test time; no binaries are committed."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image


def _exif_bytes(make: str, model: str, dt: str, subsec: str = "") -> bytes:
    """Build a minimal EXIF block using Pillow's own Exif container."""
    exif = Image.Exif()
    exif[0x010F] = make                      # Make
    exif[0x0110] = model                     # Model
    ifd = {0x9003: dt, 0x829A: (1, 200), 0x829D: (28, 10)}  # DateTimeOriginal, ExposureTime, FNumber
    if subsec:
        ifd[0x9291] = subsec                 # SubSecTimeOriginal
    exif[0x8769] = ifd                       # Exif IFD pointer
    return exif.tobytes()


@pytest.fixture
def make_jpeg():
    def _make(path: Path, *, size=(64, 48), color=(255, 0, 0),
              make="Canon", model="EOS R5", dt="2024:05:01 12:00:00",
              subsec="", quality=90, with_exif=True) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", size, color)
        # Give the image real structure so perceptual hashing has signal.
        for x in range(0, size[0], 8):
            for y in range(0, size[1], 8):
                if (x // 8 + y // 8) % 2 == 0:
                    img.paste((0, 0, 255), (x, y, min(x + 8, size[0]),
                                            min(y + 8, size[1])))
        kwargs = {"quality": quality}
        if with_exif:
            kwargs["exif"] = _exif_bytes(make, model, dt, subsec)
        img.save(path, format="JPEG", **kwargs)
        return path
    return _make
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_exif.py
from megaclean.signature import exif_key


def test_exif_key_identifies_the_shot(make_jpeg, tmp_path):
    p = make_jpeg(tmp_path / "a.jpg")
    key = exif_key(p.read_bytes())
    assert key is not None
    assert "Canon" in key and "EOS R5" in key and "2024:05:01 12:00:00" in key


def test_same_shot_re_encoded_keeps_its_key(make_jpeg, tmp_path):
    a = make_jpeg(tmp_path / "a.jpg", quality=90)
    b = make_jpeg(tmp_path / "b.jpg", quality=40)
    assert a.read_bytes() != b.read_bytes()
    assert exif_key(a.read_bytes()) == exif_key(b.read_bytes())


def test_different_shots_differ(make_jpeg, tmp_path):
    a = make_jpeg(tmp_path / "a.jpg", dt="2024:05:01 12:00:00")
    b = make_jpeg(tmp_path / "b.jpg", dt="2024:05:01 12:00:01")
    assert exif_key(a.read_bytes()) != exif_key(b.read_bytes())


def test_burst_frames_separated_by_subsecond(make_jpeg, tmp_path):
    a = make_jpeg(tmp_path / "a.jpg", subsec="10")
    b = make_jpeg(tmp_path / "b.jpg", subsec="30")
    assert exif_key(a.read_bytes()) != exif_key(b.read_bytes())


def test_no_datetime_means_no_key(make_jpeg, tmp_path):
    p = make_jpeg(tmp_path / "a.jpg", with_exif=False)
    assert exif_key(p.read_bytes()) is None


def test_truncated_head_still_parses(make_jpeg, tmp_path):
    p = make_jpeg(tmp_path / "a.jpg", size=(800, 600))
    full = p.read_bytes()
    assert exif_key(full[:65536]) == exif_key(full)


def test_garbage_returns_none():
    assert exif_key(b"not an image at all") is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_exif.py -v`
Expected: FAIL with `ImportError: cannot import name 'exif_key'`

- [ ] **Step 4: Append the implementation to `megaclean/signature.py`**

```python
import io
import logging

import exifread

log = logging.getLogger(__name__)

_EXIF_FIELDS = (
    "Image Make",
    "Image Model",
    "EXIF DateTimeOriginal",
    "EXIF SubSecTimeOriginal",
    "EXIF ExposureTime",
    "EXIF FNumber",
)


def _read_exif(head: bytes) -> dict:
    """Parse EXIF from a possibly-truncated buffer.

    exifread tolerates truncation because the APP1 segment sits at the front of
    the file, which is what makes a 64 KB range read sufficient.
    """
    try:
        return exifread.process_file(
            io.BytesIO(head), details=True, strict=False
        )
    except Exception:                       # exifread raises assorted types
        log.debug("EXIF parse failed", exc_info=True)
        return {}


def exif_key(head: bytes) -> str | None:
    """A grouping key for "the same shot", or None if it cannot be determined.

    DateTimeOriginal is required: without it the remaining fields are far too
    weak to group by. Exposure and sub-second time are included so that burst
    frames sharing a one-second timestamp stay apart.
    """
    tags = _read_exif(head)
    if not tags:
        return None
    dt = tags.get("EXIF DateTimeOriginal")
    if dt is None or not str(dt).strip():
        return None
    return "|".join(str(tags.get(f, "")).strip() for f in _EXIF_FIELDS)


def embedded_thumbnail(head: bytes) -> bytes | None:
    """The camera-embedded JPEG thumbnail, when the file carries one."""
    tags = _read_exif(head)
    thumb = tags.get("JPEGThumbnail")
    if isinstance(thumb, (bytes, bytearray)) and len(thumb) > 0:
        return bytes(thumb)
    return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_exif.py -v`
Expected: all 7 tests PASS

- [ ] **Step 6: Commit**

```bash
git add megaclean/signature.py tests/conftest.py tests/test_exif.py
git commit -m "feat: EXIF grouping key from truncated head buffer"
```

---

### Task 4: Perceptual hashing with provenance

**Files:**
- Modify: `megaclean/signature.py`
- Test: `tests/test_phash.py`

**Interfaces:**
- Consumes: `embedded_thumbnail` from Task 3.
- Produces:
  - `perceptual_hash(head: bytes) -> tuple[str, str] | None` — `(hex, provenance)` where provenance is `"thumb"` or `"full"`; prefers the embedded thumbnail, falls back to decoding the buffer, returns `None` if neither works.
  - `image_dimensions(head: bytes) -> tuple[int, int] | None`
  - `hamming_hex(a: str, b: str) -> int`

**Critical detail:** never compare a `thumb` hash to a `full` hash. JPEG thumbnail artifacts shift the hash enough to make cross-class comparison unreliable; the comparison rule is enforced in Task 8's clustering.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_phash.py
from megaclean.signature import (
    perceptual_hash, image_dimensions, hamming_hex,
)


def test_hash_has_provenance(make_jpeg, tmp_path):
    p = make_jpeg(tmp_path / "a.jpg", size=(256, 256))
    result = perceptual_hash(p.read_bytes())
    assert result is not None
    _, src = result
    assert src in ("thumb", "full")


def test_resized_copy_hashes_close(make_jpeg, tmp_path):
    from PIL import Image
    a = make_jpeg(tmp_path / "a.jpg", size=(512, 512))
    img = Image.open(a).resize((256, 256))
    b = tmp_path / "b.jpg"
    img.save(b, format="JPEG", quality=85)
    ha, sa = perceptual_hash(a.read_bytes())
    hb, sb = perceptual_hash(b.read_bytes())
    assert sa == sb
    assert hamming_hex(ha, hb) <= 6


def test_different_images_hash_far(make_jpeg, tmp_path):
    from PIL import Image
    a = make_jpeg(tmp_path / "a.jpg", size=(256, 256))
    b = tmp_path / "b.jpg"
    Image.effect_noise((256, 256), 96).convert("RGB").save(b, format="JPEG")
    ha, _ = perceptual_hash(a.read_bytes())
    hb, _ = perceptual_hash(b.read_bytes())
    assert hamming_hex(ha, hb) > 6


def test_dimensions_read_from_head(make_jpeg, tmp_path):
    p = make_jpeg(tmp_path / "a.jpg", size=(320, 240))
    assert image_dimensions(p.read_bytes()) == (320, 240)


def test_garbage_returns_none():
    assert perceptual_hash(b"nonsense") is None
    assert image_dimensions(b"nonsense") is None


def test_hamming_hex_basics():
    assert hamming_hex("00", "00") == 0
    assert hamming_hex("00", "01") == 1
    assert hamming_hex("0f", "00") == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_phash.py -v`
Expected: FAIL with `ImportError: cannot import name 'perceptual_hash'`

- [ ] **Step 3: Append the implementation to `megaclean/signature.py`**

```python
import imagehash
from PIL import Image, ImageFile

import pillow_heif

pillow_heif.register_heif_opener()
# A 64 KB head buffer is a truncated image by construction; decoding it is
# expected and the partial result is good enough for a perceptual hash.
ImageFile.LOAD_TRUNCATED_IMAGES = True


def _open(data: bytes) -> Image.Image | None:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img.convert("RGB")
    except Exception:
        log.debug("image decode failed", exc_info=True)
        return None


def perceptual_hash(head: bytes) -> tuple[str, str] | None:
    """Return (hex_hash, provenance).

    The camera-embedded thumbnail is preferred because it is fully present in
    the head buffer, whereas the main image is truncated. Hashes must only be
    compared within the same provenance class.
    """
    thumb = embedded_thumbnail(head)
    if thumb:
        img = _open(thumb)
        if img is not None:
            return str(imagehash.phash(img)), "thumb"
    img = _open(head)
    if img is not None:
        return str(imagehash.phash(img)), "full"
    return None


def image_dimensions(head: bytes) -> tuple[int, int] | None:
    try:
        with Image.open(io.BytesIO(head)) as img:
            return img.size
    except Exception:
        return None


def hamming_hex(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_phash.py -v`
Expected: all 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add megaclean/signature.py tests/test_phash.py
git commit -m "feat: perceptual hashing with thumb/full provenance"
```

---

### Task 5: Remote protocol, rclone implementation, and fake

**Files:**
- Create: `megaclean/remote.py`
- Test: `tests/test_remote.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `@dataclass(frozen=True) RemoteFile: path: str; size: int; mtime: str`
  - `class Remote(Protocol)`: `list_files(root: str) -> Iterator[RemoteFile]`, `read_range(path: str, offset: int, count: int) -> bytes`, `upload(local_path: str, dest_path: str) -> None`
  - `class RcloneRemote(remote: str, runner: Callable = subprocess.run, binary: str = "rclone")`
  - `class FakeRemote(files: dict[str, bytes])` — also records `uploads: list[tuple[str, str]]`
  - `RemoteError(Exception)`
  - `join_remote_path(root: str, rel: str) -> str` — joins a remote root and a relative path

**Critical detail:** the index stores *full* remote paths, not paths relative to
the scanned root. `read_range` and `upload` address the account, so a relative
path would resolve to nothing; `join_remote_path` is what keeps the two in step.

**Critical detail:** the tail read uses a negative `--offset`, which rclone interprets as an offset from the end of the file.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_remote.py
import json
import pytest
from megaclean.remote import RcloneRemote, FakeRemote, RemoteFile, RemoteError


class Recorder:
    """Stands in for subprocess.run and records the argv it was handed."""

    def __init__(self, stdout=b"", returncode=0, stderr=b""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        class R:
            pass
        r = R()
        r.stdout, r.stderr, r.returncode = self.stdout, self.stderr, self.returncode
        return r


def test_list_files_parses_lsjson():
    payload = json.dumps([
        {"Path": "a.jpg", "Size": 100, "ModTime": "2024-01-01T00:00:00Z", "IsDir": False},
        {"Path": "sub/b.jpg", "Size": 200, "ModTime": "2024-01-02T00:00:00Z", "IsDir": False},
    ]).encode()
    rec = Recorder(stdout=payload)
    files = list(RcloneRemote("mega", runner=rec).list_files("Photos"))
    assert files == [
        RemoteFile("a.jpg", 100, "2024-01-01T00:00:00Z"),
        RemoteFile("sub/b.jpg", 200, "2024-01-02T00:00:00Z"),
    ]
    assert "lsjson" in rec.calls[0]
    assert "mega:Photos" in rec.calls[0]


def test_read_range_uses_offset_and_count():
    rec = Recorder(stdout=b"bytes")
    assert RcloneRemote("mega", runner=rec).read_range("a.jpg", 0, 65536) == b"bytes"
    argv = rec.calls[0]
    assert argv[argv.index("--offset") + 1] == "0"
    assert argv[argv.index("--count") + 1] == "65536"


def test_negative_offset_reads_the_tail():
    rec = Recorder(stdout=b"tail")
    RcloneRemote("mega", runner=rec).read_range("a.jpg", -65536, 65536)
    argv = rec.calls[0]
    assert argv[argv.index("--offset") + 1] == "-65536"


def test_nonzero_exit_raises_remote_error():
    rec = Recorder(returncode=1, stderr=b"no such file")
    with pytest.raises(RemoteError, match="no such file"):
        RcloneRemote("mega", runner=rec).read_range("missing.jpg", 0, 10)


def test_upload_uses_copyto_with_destination():
    rec = Recorder()
    RcloneRemote("mega", runner=rec).upload("/local/a.jpg", "Photos/2024/a.jpg")
    argv = rec.calls[0]
    assert "copyto" in argv
    assert "/local/a.jpg" in argv
    assert "mega:Photos/2024/a.jpg" in argv


def test_fake_remote_serves_ranges_and_records_uploads(tmp_path):
    data = bytes(range(256)) * 4
    fake = FakeRemote({"a.jpg": data})
    assert list(fake.list_files(""))[0].size == len(data)
    assert fake.read_range("a.jpg", 0, 10) == data[:10]
    assert fake.read_range("a.jpg", -10, 10) == data[-10:]
    fake.upload("/local/b.jpg", "Photos/b.jpg")
    assert fake.uploads == [("/local/b.jpg", "Photos/b.jpg")]
    assert "Photos/b.jpg" in fake.files


def test_fake_remote_raises_for_missing_path():
    with pytest.raises(RemoteError):
        FakeRemote({}).read_range("nope.jpg", 0, 1)


def test_join_remote_path():
    from megaclean.remote import join_remote_path
    assert join_remote_path("Photos", "a.jpg") == "Photos/a.jpg"
    assert join_remote_path("Photos/", "sub/a.jpg") == "Photos/sub/a.jpg"
    assert join_remote_path("", "a.jpg") == "a.jpg"
    assert join_remote_path("/Photos/", "a.jpg") == "Photos/a.jpg"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_remote.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'megaclean.remote'`

- [ ] **Step 3: Write the implementation**

```python
# megaclean/remote.py
"""All network access, behind a protocol with an in-memory fake.

rclone is the transport for every operation. MEGAcmd cannot serve partial
reads, so fingerprinting requires rclone regardless of how the account was
authenticated.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Protocol


class RemoteError(Exception):
    """An rclone invocation failed."""


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int
    mtime: str


def join_remote_path(root: str, rel: str) -> str:
    """Join a remote root and a relative path into a full account path."""
    root = root.strip("/")
    return f"{root}/{rel}" if root else rel


class Remote(Protocol):
    def list_files(self, root: str) -> Iterator[RemoteFile]: ...
    def read_range(self, path: str, offset: int, count: int) -> bytes: ...
    def upload(self, local_path: str, dest_path: str) -> None: ...


class RcloneRemote:
    def __init__(self, remote: str, runner: Callable = subprocess.run,
                 binary: str = "rclone") -> None:
        self.remote = remote.rstrip(":")
        self.runner = runner
        self.binary = binary

    def _target(self, path: str) -> str:
        return f"{self.remote}:{path}"

    def _run(self, argv: list[str]) -> bytes:
        result = self.runner(argv, capture_output=True)
        if result.returncode != 0:
            stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
            raise RemoteError(f"{' '.join(argv)} failed: {stderr}")
        return result.stdout or b""

    def list_files(self, root: str) -> Iterator[RemoteFile]:
        out = self._run([
            self.binary, "lsjson", "--recursive", "--files-only",
            self._target(root),
        ])
        for entry in json.loads(out or b"[]"):
            if entry.get("IsDir"):
                continue
            yield RemoteFile(entry["Path"], int(entry["Size"]),
                             entry.get("ModTime", ""))

    def read_range(self, path: str, offset: int, count: int) -> bytes:
        """Read `count` bytes at `offset`; a negative offset counts from the end."""
        return self._run([
            self.binary, "cat", "--offset", str(offset), "--count", str(count),
            self._target(path),
        ])

    def upload(self, local_path: str, dest_path: str) -> None:
        self._run([self.binary, "copyto", local_path, self._target(dest_path)])


@dataclass
class FakeRemote:
    """In-memory Remote so every flow is testable with no network."""

    files: dict[str, bytes]
    uploads: list[tuple[str, str]] = field(default_factory=list)

    def list_files(self, root: str) -> Iterator[RemoteFile]:
        prefix = root.rstrip("/") + "/" if root.rstrip("/") else ""
        for path, data in sorted(self.files.items()):
            if path.startswith(prefix):
                yield RemoteFile(path[len(prefix):], len(data),
                                 "2024-01-01T00:00:00Z")

    def read_range(self, path: str, offset: int, count: int) -> bytes:
        if path not in self.files:
            raise RemoteError(f"no such remote file: {path}")
        data = self.files[path]
        if offset < 0:
            return data[offset:][:count]
        return data[offset:offset + count]

    def upload(self, local_path: str, dest_path: str) -> None:
        from pathlib import Path
        self.uploads.append((local_path, dest_path))
        self.files[dest_path] = Path(local_path).read_bytes()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_remote.py -v`
Expected: all 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add megaclean/remote.py tests/test_remote.py
git commit -m "feat: Remote protocol with rclone transport and in-memory fake"
```

---

### Task 6: Fingerprinting engine

**Files:**
- Create: `megaclean/fingerprint.py`
- Test: `tests/test_fingerprint.py`

**Interfaces:**
- Consumes: `signature.py` (Task 2-4), `remote.py` (Task 5), `index.py` (Task 1).
- Produces:
  - `@dataclass(frozen=True) Fingerprint: sig: str; exif_key: str | None; phash: str | None; phash_src: str | None; width: int | None; height: int | None`
  - `fingerprint_from_parts(size: int, head: bytes, tail: bytes) -> Fingerprint`
  - `fetch_fingerprint(remote: Remote, path: str, size: int) -> Fingerprint`
  - `run_fingerprint(conn, remote: Remote, scope: str, paths: list[str], *, workers: int = 8, sizes: dict[str, int], on_progress=None) -> tuple[int, int]` returning `(succeeded, failed)`

**Critical detail:** a file at or below `HEAD_BYTES + TAIL_BYTES` needs no tail request — issuing one would double the request count for the majority of thumbnails and small images.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fingerprint.py
import pytest
from megaclean.fingerprint import (
    Fingerprint, fingerprint_from_parts, fetch_fingerprint, run_fingerprint,
)
from megaclean.index import open_index, upsert_node, iter_nodes
from megaclean.remote import FakeRemote, RemoteError
from megaclean.signature import signature_of_bytes


class CountingRemote(FakeRemote):
    def __init__(self, files):
        super().__init__(files)
        self.reads = []

    def read_range(self, path, offset, count):
        self.reads.append((path, offset, count))
        return super().read_range(path, offset, count)


def test_fingerprint_matches_whole_file_signature(make_jpeg, tmp_path):
    data = (make_jpeg(tmp_path / "a.jpg", size=(400, 300))).read_bytes()
    remote = FakeRemote({"a.jpg": data})
    fp = fetch_fingerprint(remote, "a.jpg", len(data))
    assert fp.sig == signature_of_bytes(data)
    assert fp.exif_key is not None
    assert fp.width == 400 and fp.height == 300


def test_small_file_skips_the_tail_request(make_jpeg, tmp_path):
    data = (make_jpeg(tmp_path / "a.jpg", size=(32, 32))).read_bytes()
    assert len(data) <= 131072
    remote = CountingRemote({"a.jpg": data})
    fetch_fingerprint(remote, "a.jpg", len(data))
    assert len(remote.reads) == 1


def test_large_file_issues_head_and_tail_requests():
    data = b"\xff\xd8" + b"x" * 200000
    remote = CountingRemote({"a.jpg": data})
    fetch_fingerprint(remote, "a.jpg", len(data))
    assert len(remote.reads) == 2
    assert remote.reads[1][1] == -65536


def test_run_fingerprint_persists_results(make_jpeg, tmp_path):
    a = (make_jpeg(tmp_path / "a.jpg")).read_bytes()
    b = (make_jpeg(tmp_path / "b.jpg", dt="2024:06:01 09:00:00")).read_bytes()
    remote = FakeRemote({"a.jpg": a, "b.jpg": b})
    conn = open_index(tmp_path / "i.db")
    upsert_node(conn, "remote", "a.jpg", len(a), "t")
    upsert_node(conn, "remote", "b.jpg", len(b), "t")
    ok, failed = run_fingerprint(conn, remote, "remote", ["a.jpg", "b.jpg"],
                                 workers=2, sizes={"a.jpg": len(a), "b.jpg": len(b)})
    assert (ok, failed) == (2, 0)
    rows = {r["path"]: r for r in iter_nodes(conn, "remote")}
    assert rows["a.jpg"]["sig"] == signature_of_bytes(a)
    assert rows["a.jpg"]["exif_key"] != rows["b.jpg"]["exif_key"]


def test_failures_are_recorded_not_raised(tmp_path):
    remote = FakeRemote({})
    conn = open_index(tmp_path / "i.db")
    upsert_node(conn, "remote", "gone.jpg", 10, "t")
    ok, failed = run_fingerprint(conn, remote, "remote", ["gone.jpg"],
                                 workers=1, sizes={"gone.jpg": 10})
    assert (ok, failed) == (0, 1)
    row = next(iter_nodes(conn, "remote"))
    assert row["sig"] is None
    assert "no such remote file" in row["error"]


def test_rerun_skips_already_fingerprinted(make_jpeg, tmp_path):
    data = (make_jpeg(tmp_path / "a.jpg")).read_bytes()
    remote = CountingRemote({"a.jpg": data})
    conn = open_index(tmp_path / "i.db")
    upsert_node(conn, "remote", "a.jpg", len(data), "t")
    from megaclean.index import pending_paths
    sizes = {"a.jpg": len(data)}
    run_fingerprint(conn, remote, "remote", pending_paths(conn, "remote"),
                    workers=1, sizes=sizes)
    before = len(remote.reads)
    run_fingerprint(conn, remote, "remote", pending_paths(conn, "remote"),
                    workers=1, sizes=sizes)
    assert len(remote.reads) == before
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fingerprint.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'megaclean.fingerprint'`

- [ ] **Step 3: Write the implementation**

```python
# megaclean/fingerprint.py
"""Fetch the bytes needed for content identity, concurrently and resumably."""
from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from .index import set_error, set_signature
from .remote import Remote, RemoteError
from .signature import (
    HEAD_BYTES, TAIL_BYTES, content_signature, exif_key, image_dimensions,
    needs_tail_read, perceptual_hash,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Fingerprint:
    sig: str
    exif_key: str | None
    phash: str | None
    phash_src: str | None
    width: int | None
    height: int | None


def fingerprint_from_parts(size: int, head: bytes, tail: bytes) -> Fingerprint:
    ph = perceptual_hash(head)
    dims = image_dimensions(head)
    return Fingerprint(
        sig=content_signature(size, head, tail),
        exif_key=exif_key(head),
        phash=ph[0] if ph else None,
        phash_src=ph[1] if ph else None,
        width=dims[0] if dims else None,
        height=dims[1] if dims else None,
    )


def fetch_fingerprint(remote: Remote, path: str, size: int) -> Fingerprint:
    head = remote.read_range(path, 0, HEAD_BYTES)
    tail = remote.read_range(path, -TAIL_BYTES, TAIL_BYTES) \
        if needs_tail_read(size) else b""
    return fingerprint_from_parts(size, head, tail)


def _fetch_with_backoff(remote: Remote, path: str, size: int) -> Fingerprint:
    """MEGA throttles aggressive clients, so back off rather than hammer it."""
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return fetch_fingerprint(remote, path, size)
        except RemoteError as exc:
            last = exc
            if attempt == MAX_ATTEMPTS - 1:
                break
            time.sleep((2 ** attempt) + random.random())
    raise last  # type: ignore[misc]


def run_fingerprint(conn, remote: Remote, scope: str, paths: list[str], *,
                    workers: int = 8, sizes: dict[str, int],
                    on_progress: Callable[[str, bool], None] | None = None
                    ) -> tuple[int, int]:
    """Fingerprint `paths`, writing each result as it lands.

    Results are committed one at a time so an interrupted run loses only the
    work in flight; the next run picks up whatever is still pending.
    """
    ok = failed = 0
    if not paths:
        return (0, 0)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_fetch_with_backoff, remote, p, sizes[p]): p
            for p in paths
        }
        for future in as_completed(futures):
            path = futures[future]
            try:
                fp = future.result()
            except Exception as exc:
                set_error(conn, scope, path, str(exc))
                failed += 1
                log.warning("fingerprint failed for %s: %s", path, exc)
                if on_progress:
                    on_progress(path, False)
                continue
            set_signature(conn, scope, path, sig=fp.sig, exif_key=fp.exif_key,
                          phash=fp.phash, phash_src=fp.phash_src,
                          width=fp.width, height=fp.height)
            ok += 1
            if on_progress:
                on_progress(path, True)
    return (ok, failed)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_fingerprint.py -v`
Expected: all 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add megaclean/fingerprint.py tests/test_fingerprint.py
git commit -m "feat: concurrent resumable fingerprinting with backoff"
```

---

### Task 7: Clustering and keeper selection

**Files:**
- Create: `megaclean/dupes.py`
- Test: `tests/test_dupes.py`

**Interfaces:**
- Consumes: `hamming_hex` from Task 4.
- Produces:
  - `@dataclass(frozen=True) Node: path: str; size: int; sig: str; exif_key: str | None; phash: str | None; phash_src: str | None; width: int | None; height: int | None; mtime: str`
  - `@dataclass(frozen=True) Cluster: kind: str; members: tuple[Node, ...]; keeper: Node` where `kind` is `"exact"` or `"variant"`
  - `choose_keeper(nodes: Sequence[Node]) -> Node`
  - `cluster_nodes(nodes: Sequence[Node], *, phash_threshold: int = 6) -> list[Cluster]`
  - `class BKTree`: `add(key: str) -> None`, `query(key: str, max_distance: int) -> list[str]`

**Critical detail:** pairwise perceptual comparison is quadratic and untenable at 100k files, hence the BK-tree. And hashes are only compared within the same provenance class, so build one tree per class.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dupes.py
from megaclean.dupes import Node, BKTree, choose_keeper, cluster_nodes


def node(path, **kw):
    base = dict(size=100, sig="s-" + path, exif_key=None, phash=None,
                phash_src=None, width=None, height=None, mtime="2024-01-01")
    base.update(kw)
    return Node(path=path, **base)


def test_bktree_finds_near_neighbours():
    tree = BKTree()
    for h in ("0000000000000000", "0000000000000003", "ffffffffffffffff"):
        tree.add(h)
    near = tree.query("0000000000000000", 6)
    assert set(near) == {"0000000000000000", "0000000000000003"}


def test_bktree_empty_query_is_safe():
    assert BKTree().query("00", 6) == []


def test_identical_signatures_form_an_exact_cluster():
    nodes = [node("a.jpg", sig="same"), node("b.jpg", sig="same"),
             node("c.jpg", sig="other")]
    clusters = cluster_nodes(nodes)
    assert len(clusters) == 1
    assert clusters[0].kind == "exact"
    assert {n.path for n in clusters[0].members} == {"a.jpg", "b.jpg"}


def test_singletons_are_not_reported():
    assert cluster_nodes([node("a.jpg"), node("b.jpg")]) == []


def test_shared_exif_key_forms_a_variant_cluster():
    nodes = [node("a.jpg", exif_key="canon|r5|2024", size=900),
             node("b.jpg", exif_key="canon|r5|2024", size=300)]
    clusters = cluster_nodes(nodes)
    assert len(clusters) == 1
    assert clusters[0].kind == "variant"


def test_close_phash_forms_a_variant_cluster():
    nodes = [node("a.jpg", phash="0000000000000000", phash_src="thumb"),
             node("b.jpg", phash="0000000000000003", phash_src="thumb")]
    assert cluster_nodes(nodes)[0].kind == "variant"


def test_phash_across_provenance_is_never_compared():
    nodes = [node("a.jpg", phash="0000000000000000", phash_src="thumb"),
             node("b.jpg", phash="0000000000000000", phash_src="full")]
    assert cluster_nodes(nodes) == []


def test_distant_phash_does_not_cluster():
    nodes = [node("a.jpg", phash="0000000000000000", phash_src="thumb"),
             node("b.jpg", phash="ffffffffffffffff", phash_src="thumb")]
    assert cluster_nodes(nodes) == []


def test_exact_plus_variant_merges_into_one_variant_cluster():
    nodes = [node("a.jpg", sig="same", exif_key="k"),
             node("b.jpg", sig="same", exif_key="k"),
             node("c.jpg", sig="different", exif_key="k")]
    clusters = cluster_nodes(nodes)
    assert len(clusters) == 1
    assert clusters[0].kind == "variant"
    assert len(clusters[0].members) == 3


def test_keeper_prefers_largest_pixel_area():
    a = node("a.jpg", width=100, height=100, size=999999)
    b = node("b.jpg", width=400, height=400, size=10)
    assert choose_keeper([a, b]).path == "b.jpg"


def test_keeper_falls_back_to_size_then_mtime_then_depth():
    a = node("deep/nested/a.jpg", size=100, mtime="2024-01-02")
    b = node("b.jpg", size=100, mtime="2024-01-01")
    assert choose_keeper([a, b]).path == "b.jpg"
    c = node("x/c.jpg", size=100, mtime="2024-01-01")
    d = node("d.jpg", size=100, mtime="2024-01-01")
    assert choose_keeper([c, d]).path == "d.jpg"


def test_keeper_is_deterministic_regardless_of_input_order():
    nodes = [node(f"{c}.jpg", size=100, mtime="2024-01-01") for c in "abcd"]
    assert choose_keeper(nodes).path == choose_keeper(list(reversed(nodes))).path
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dupes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'megaclean.dupes'`

- [ ] **Step 3: Write the implementation**

```python
# megaclean/dupes.py
"""Group nodes into duplicate and variant clusters. Reports only; deletes never."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from .signature import hamming_hex


@dataclass(frozen=True)
class Node:
    path: str
    size: int
    sig: str
    exif_key: str | None
    phash: str | None
    phash_src: str | None
    width: int | None
    height: int | None
    mtime: str


@dataclass(frozen=True)
class Cluster:
    kind: str                      # "exact" | "variant"
    members: tuple[Node, ...]
    keeper: Node


class BKTree:
    """Metric tree over Hamming distance, so near-neighbour lookup is not O(n^2)."""

    def __init__(self) -> None:
        self._root: str | None = None
        self._children: dict[str, dict[int, str]] = {}

    def add(self, key: str) -> None:
        if self._root is None:
            self._root, self._children[key] = key, {}
            return
        node = self._root
        while True:
            dist = hamming_hex(key, node)
            if dist == 0:
                return                      # already present
            child = self._children[node].get(dist)
            if child is None:
                self._children[node][dist] = key
                self._children.setdefault(key, {})
                return
            node = child

    def query(self, key: str, max_distance: int) -> list[str]:
        if self._root is None:
            return []
        found: list[str] = []
        stack = [self._root]
        while stack:
            node = stack.pop()
            dist = hamming_hex(key, node)
            if dist <= max_distance:
                found.append(node)
            for edge, child in self._children[node].items():
                if dist - max_distance <= edge <= dist + max_distance:
                    stack.append(child)
        return found


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:      # path compression
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def choose_keeper(nodes: Sequence[Node]) -> Node:
    """Deterministic: largest pixel area, then bytes, then oldest, then shallowest."""
    def rank(n: Node):
        area = (n.width or 0) * (n.height or 0)
        return (-area, -n.size, n.mtime or "", n.path.count("/"), n.path)
    return min(nodes, key=rank)


def cluster_nodes(nodes: Sequence[Node], *,
                  phash_threshold: int = 6) -> list[Cluster]:
    by_path = {n.path: n for n in nodes}
    uf = _UnionFind()
    for path in by_path:
        uf.find(path)

    for key in ("sig", "exif_key"):
        buckets: dict[str, list[str]] = defaultdict(list)
        for n in nodes:
            value = getattr(n, key)
            if value:
                buckets[value].append(n.path)
        for paths in buckets.values():
            for other in paths[1:]:
                uf.union(paths[0], other)

    # One tree per provenance class: thumbnail and full-image hashes describe
    # the same photo differently and must never be compared to each other.
    for src in ("thumb", "full"):
        members = [n for n in nodes if n.phash and n.phash_src == src]
        if len(members) < 2:
            continue
        tree, owners = BKTree(), defaultdict(list)
        for n in members:
            tree.add(n.phash)
            owners[n.phash].append(n.path)
        for n in members:
            for neighbour in tree.query(n.phash, phash_threshold):
                for path in owners[neighbour]:
                    uf.union(n.path, path)

    groups: dict[str, list[Node]] = defaultdict(list)
    for n in nodes:
        groups[uf.find(n.path)].append(n)

    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda n: n.path)
        kind = "exact" if len({m.sig for m in members}) == 1 else "variant"
        clusters.append(Cluster(kind=kind, members=tuple(members),
                                keeper=choose_keeper(members)))
    clusters.sort(key=lambda c: (c.kind, c.members[0].path))
    return clusters
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_dupes.py -v`
Expected: all 12 tests PASS

- [ ] **Step 5: Commit**

```bash
git add megaclean/dupes.py tests/test_dupes.py
git commit -m "feat: union-find clustering over signature, EXIF and BK-tree phash"
```

---

### Task 8: Reports

**Files:**
- Create: `megaclean/report.py`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `Cluster`, `Node` from Task 7.
- Produces:
  - `write_csv(clusters: Sequence[Cluster], path: Path) -> None` — columns `cluster_id,kind,role,path,size,width,height,mtime`
  - `write_html(clusters: Sequence[Cluster], path: Path, *, account: str = "", generated: str | None = None) -> None`
  - `summarize(clusters: Sequence[Cluster]) -> dict[str, int]` with keys `clusters`, `exact_clusters`, `variant_clusters`, `redundant_files`, `redundant_bytes`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report.py
import csv
from megaclean.dupes import Cluster, Node
from megaclean.report import write_csv, write_html, summarize


def n(path, size=100, sig="s"):
    return Node(path=path, size=size, sig=sig, exif_key=None, phash=None,
                phash_src=None, width=10, height=10, mtime="2024-01-01")


def clusters():
    a, b = n("a.jpg", 500, "same"), n("b.jpg", 500, "same")
    c, d = n("c.jpg", 300, "x"), n("d.jpg", 200, "y")
    return [Cluster("exact", (a, b), a), Cluster("variant", (c, d), c)]


def test_summary_counts_redundancy_not_totals():
    s = summarize(clusters())
    assert s["clusters"] == 2
    assert s["exact_clusters"] == 1
    assert s["variant_clusters"] == 1
    assert s["redundant_files"] == 2          # one non-keeper per cluster
    assert s["redundant_bytes"] == 700        # 500 + 200


def test_csv_marks_keeper_and_duplicate_roles(tmp_path):
    out = tmp_path / "r.csv"
    write_csv(clusters(), out)
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 4
    assert {r["role"] for r in rows} == {"keeper", "duplicate"}
    keepers = [r["path"] for r in rows if r["role"] == "keeper"]
    assert sorted(keepers) == ["a.jpg", "c.jpg"]
    assert len({r["cluster_id"] for r in rows}) == 2


def test_html_is_self_contained_and_escapes_paths(tmp_path):
    out = tmp_path / "r.html"
    weird = Node(path="<script>x</script>.jpg", size=1, sig="z", exif_key=None,
                 phash=None, phash_src=None, width=1, height=1, mtime="t")
    write_html([Cluster("exact", (weird, n("ok.jpg")), weird)], out)
    text = out.read_text()
    assert "<script>x</script>.jpg" not in text
    assert "&lt;script&gt;" in text
    assert "http://" not in text and "https://" not in text
    assert "exact" in text


def test_empty_report_is_valid(tmp_path):
    write_csv([], tmp_path / "r.csv")
    write_html([], tmp_path / "r.html")
    assert summarize([])["clusters"] == 0
    assert "No duplicates" in (tmp_path / "r.html").read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_report.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'megaclean.report'`

- [ ] **Step 3: Write the implementation**

```python
# megaclean/report.py
"""Reporting. The report suggests a keeper; it never acts on one."""
from __future__ import annotations

import csv
import datetime as _dt
from collections.abc import Sequence
from html import escape
from pathlib import Path

from .dupes import Cluster

_CSV_COLUMNS = ["cluster_id", "kind", "role", "path", "size", "width",
                "height", "mtime"]


def summarize(clusters: Sequence[Cluster]) -> dict[str, int]:
    """Redundancy, not totals: what could be reclaimed if every keeper stayed."""
    redundant = [m for c in clusters for m in c.members if m.path != c.keeper.path]
    return {
        "clusters": len(clusters),
        "exact_clusters": sum(1 for c in clusters if c.kind == "exact"),
        "variant_clusters": sum(1 for c in clusters if c.kind == "variant"),
        "redundant_files": len(redundant),
        "redundant_bytes": sum(m.size for m in redundant),
    }


def write_csv(clusters: Sequence[Cluster], path: Path) -> None:
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for i, cluster in enumerate(clusters, start=1):
            for member in cluster.members:
                writer.writerow({
                    "cluster_id": i,
                    "kind": cluster.kind,
                    "role": "keeper" if member.path == cluster.keeper.path
                            else "duplicate",
                    "path": member.path,
                    "size": member.size,
                    "width": member.width or "",
                    "height": member.height or "",
                    "mtime": member.mtime,
                })


def _human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def write_html(clusters: Sequence[Cluster], path: Path, *, account: str = "",
               generated: str | None = None) -> None:
    """A single self-contained file: no scripts, no external requests."""
    stats = summarize(clusters)
    when = generated or _dt.datetime.now().isoformat(timespec="seconds")
    parts = [
        "<!doctype html><meta charset='utf-8'>",
        "<title>mega-clean duplicate report</title>",
        "<style>body{font:14px/1.5 system-ui,sans-serif;margin:2rem;max-width:70rem}"
        "table{border-collapse:collapse;width:100%;margin-bottom:1.5rem}"
        "td,th{border-bottom:1px solid #ddd;padding:.35rem .6rem;text-align:left}"
        ".keeper{font-weight:600}.dup{color:#a33}"
        ".exact{background:#eef7ee}.variant{background:#fdf6e3}"
        "h2{margin-top:2rem;font-size:1rem}</style>",
        f"<h1>Duplicate report{' — ' + escape(account) if account else ''}</h1>",
        f"<p>Generated {escape(when)}. {stats['clusters']} clusters "
        f"({stats['exact_clusters']} exact, {stats['variant_clusters']} variant). "
        f"{stats['redundant_files']} redundant files, "
        f"{_human(stats['redundant_bytes'])} reclaimable.</p>",
        "<p><em>Variant clusters are a heuristic. Nothing here has been "
        "deleted; this tool has no delete capability.</em></p>",
    ]
    if not clusters:
        parts.append("<p>No duplicates found.</p>")
    for i, cluster in enumerate(clusters, start=1):
        parts.append(f"<h2 class='{cluster.kind}'>Cluster {i} — "
                     f"{cluster.kind}</h2>")
        parts.append("<table><tr><th>Role</th><th>Path</th><th>Size</th>"
                     "<th>Dimensions</th><th>Modified</th></tr>")
        for member in cluster.members:
            keeper = member.path == cluster.keeper.path
            dims = (f"{member.width}×{member.height}"
                    if member.width and member.height else "—")
            parts.append(
                f"<tr class='{'keeper' if keeper else 'dup'}'>"
                f"<td>{'keep' if keeper else 'duplicate'}</td>"
                f"<td>{escape(member.path)}</td>"
                f"<td>{_human(member.size)}</td>"
                f"<td>{dims}</td><td>{escape(member.mtime or '')}</td></tr>"
            )
        parts.append("</table>")
    Path(path).write_text("\n".join(parts), encoding="utf-8")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_report.py -v`
Expected: all 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add megaclean/report.py tests/test_report.py
git commit -m "feat: self-contained HTML and CSV duplicate reports"
```

---

### Task 9: Local folder scanning

**Files:**
- Create: `megaclean/local.py`
- Test: `tests/test_local.py`

**Interfaces:**
- Consumes: `fingerprint_from_parts` (Task 6), `HEAD_BYTES`/`TAIL_BYTES`/`needs_tail_read` (Task 2), index writers (Task 1).
- Produces:
  - `IMAGE_EXTENSIONS: frozenset[str]`, `VIDEO_EXTENSIONS: frozenset[str]`
  - `local_scope(root: Path) -> str` — returns `f"local:{root.resolve()}"`
  - `iter_media(root: Path, *, include_video: bool = False) -> Iterator[Path]`
  - `read_parts(path: Path) -> tuple[int, bytes, bytes]`
  - `scan_local(conn, root: Path, *, include_video: bool = False, on_progress=None) -> tuple[int, int]` returning `(scanned, failed)`

**Critical detail:** local files must go through the identical `fingerprint_from_parts` code path as remote ones — same head/tail slices, same perceptual-hash provenance rules. Hashing local files any other way would make the two sides incomparable, which is the whole point of the index.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_local.py
import pytest
from megaclean.index import open_index, iter_nodes
from megaclean.local import (
    IMAGE_EXTENSIONS, iter_media, local_scope, read_parts, scan_local,
)
from megaclean.fingerprint import fingerprint_from_parts
from megaclean.signature import signature_of_bytes


def test_iter_media_filters_by_extension(make_jpeg, tmp_path):
    make_jpeg(tmp_path / "a.jpg")
    make_jpeg(tmp_path / "sub" / "b.JPG")
    (tmp_path / "notes.txt").write_text("hi")
    (tmp_path / "clip.mp4").write_bytes(b"\x00")
    found = {p.name for p in iter_media(tmp_path)}
    assert found == {"a.jpg", "b.JPG"}


def test_include_video_opt_in(make_jpeg, tmp_path):
    make_jpeg(tmp_path / "a.jpg")
    (tmp_path / "clip.mp4").write_bytes(b"\x00")
    found = {p.name for p in iter_media(tmp_path, include_video=True)}
    assert found == {"a.jpg", "clip.mp4"}


def test_hidden_files_are_skipped(make_jpeg, tmp_path):
    make_jpeg(tmp_path / "a.jpg")
    make_jpeg(tmp_path / ".hidden.jpg")
    make_jpeg(tmp_path / ".cache" / "c.jpg")
    assert {p.name for p in iter_media(tmp_path)} == {"a.jpg"}


def test_read_parts_matches_whole_file_signature(make_jpeg, tmp_path):
    p = make_jpeg(tmp_path / "a.jpg", size=(400, 400))
    size, head, tail = read_parts(p)
    fp = fingerprint_from_parts(size, head, tail)
    assert fp.sig == signature_of_bytes(p.read_bytes())


def test_scan_local_records_relative_paths(make_jpeg, tmp_path):
    make_jpeg(tmp_path / "a.jpg")
    make_jpeg(tmp_path / "sub" / "b.jpg", dt="2024:07:01 10:00:00")
    conn = open_index(tmp_path / "i.db")
    scanned, failed = scan_local(conn, tmp_path)
    assert (scanned, failed) == (2, 0)
    paths = [r["path"] for r in iter_nodes(conn, local_scope(tmp_path))]
    assert paths == ["a.jpg", "sub/b.jpg"]


def test_scan_local_populates_signatures(make_jpeg, tmp_path):
    p = make_jpeg(tmp_path / "a.jpg")
    conn = open_index(tmp_path / "i.db")
    scan_local(conn, tmp_path)
    row = next(iter_nodes(conn, local_scope(tmp_path)))
    assert row["sig"] == signature_of_bytes(p.read_bytes())
    assert row["exif_key"] is not None


def test_unreadable_file_is_recorded_as_error(tmp_path):
    bad = tmp_path / "broken.jpg"
    bad.write_bytes(b"")
    bad.chmod(0o000)
    conn = open_index(tmp_path / "i.db")
    try:
        scanned, failed = scan_local(conn, tmp_path)
    finally:
        bad.chmod(0o644)
    assert failed == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_local.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'megaclean.local'`

- [ ] **Step 3: Write the implementation**

```python
# megaclean/local.py
"""Scan a local folder using the identical identity code path as the remote side."""
from __future__ import annotations

import datetime as _dt
import logging
from collections.abc import Callable, Iterator
from pathlib import Path

from .fingerprint import fingerprint_from_parts
from .index import set_error, set_signature, upsert_node
from .signature import HEAD_BYTES, TAIL_BYTES, needs_tail_read

log = logging.getLogger(__name__)

IMAGE_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp",
    ".gif", ".bmp", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2",
})
VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mov", ".avi", ".m4v", ".mts", ".3gp", ".mkv",
})


def local_scope(root: Path) -> str:
    return f"local:{Path(root).resolve()}"


def iter_media(root: Path, *, include_video: bool = False) -> Iterator[Path]:
    allowed = IMAGE_EXTENSIONS | (VIDEO_EXTENSIONS if include_video else frozenset())
    root = Path(root)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if path.suffix.lower() in allowed:
            yield path


def read_parts(path: Path) -> tuple[int, bytes, bytes]:
    """Read exactly the byte ranges the remote side fetches, and no more."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(HEAD_BYTES)
        if not needs_tail_read(size):
            return size, head, b""
        fh.seek(-TAIL_BYTES, 2)
        return size, head, fh.read(TAIL_BYTES)


def scan_local(conn, root: Path, *, include_video: bool = False,
               on_progress: Callable[[Path, bool], None] | None = None
               ) -> tuple[int, int]:
    root = Path(root).resolve()
    scope = local_scope(root)
    scanned = failed = 0
    for path in iter_media(root, include_video=include_video):
        rel = path.relative_to(root).as_posix()
        try:
            stat = path.stat()
            mtime = _dt.datetime.fromtimestamp(
                stat.st_mtime, _dt.timezone.utc
            ).isoformat(timespec="seconds")
            upsert_node(conn, scope, rel, stat.st_size, mtime)
            size, head, tail = read_parts(path)
            fp = fingerprint_from_parts(size, head, tail)
            set_signature(conn, scope, rel, sig=fp.sig, exif_key=fp.exif_key,
                          phash=fp.phash, phash_src=fp.phash_src,
                          width=fp.width, height=fp.height)
            scanned += 1
            if on_progress:
                on_progress(path, True)
        except OSError as exc:
            upsert_node(conn, scope, rel, 0, "")
            set_error(conn, scope, rel, str(exc))
            failed += 1
            log.warning("could not read %s: %s", path, exc)
            if on_progress:
                on_progress(path, False)
    return scanned, failed
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_local.py -v`
Expected: all 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add megaclean/local.py tests/test_local.py
git commit -m "feat: local folder scanning sharing the remote identity path"
```

---

### Task 10: Upload planner

**Files:**
- Create: `megaclean/planner.py`
- Test: `tests/test_planner.py`

**Interfaces:**
- Consumes: `Node` (Task 7), `hamming_hex` (Task 4).
- Produces:
  - `@dataclass(frozen=True) PlanEntry: local_path: str; size: int; sig: str; action: str; dest_path: str; remote_matches: tuple[str, ...]; reason: str` where `action` is `"upload"`, `"skip"` or `"review"`
  - `build_plan(local_nodes: Sequence[Node], remote_nodes: Sequence[Node], *, local_root: str, dest_root: str, phash_threshold: int = 6) -> list[PlanEntry]`
  - `write_plan(entries, path: Path, *, local_root: str, dest_root: str, remote: str) -> None` (JSON)
  - `read_plan(path: Path) -> tuple[list[PlanEntry], dict]` returning entries and the header metadata

**Critical detail:** a `review` entry is never skipped automatically. A variant match is not strong enough evidence to silently drop a local photo; the user decides.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_planner.py
import json
from megaclean.dupes import Node
from megaclean.planner import build_plan, write_plan, read_plan


def node(path, sig="s", **kw):
    base = dict(size=100, exif_key=None, phash=None, phash_src=None,
                width=None, height=None, mtime="2024-01-01")
    base.update(kw)
    return Node(path=path, sig=sig, **base)


def test_missing_file_is_planned_for_upload():
    plan = build_plan([node("a.jpg", "sig-a")], [], local_root="/pics",
                      dest_root="Photos")
    assert len(plan) == 1
    assert plan[0].action == "upload"
    assert plan[0].dest_path == "Photos/a.jpg"
    assert plan[0].remote_matches == ()


def test_nested_paths_are_mirrored():
    plan = build_plan([node("2024/spring/a.jpg", "sig-a")], [],
                      local_root="/pics", dest_root="Photos")
    assert plan[0].dest_path == "Photos/2024/spring/a.jpg"


def test_exact_match_anywhere_in_the_account_is_skipped():
    local = [node("a.jpg", "same")]
    remote = [node("Backup/old/renamed.jpg", "same")]
    plan = build_plan(local, remote, local_root="/pics", dest_root="Photos")
    assert plan[0].action == "skip"
    assert plan[0].remote_matches == ("Backup/old/renamed.jpg",)


def test_skip_records_every_place_the_content_lives():
    local = [node("a.jpg", "same")]
    remote = [node("x/1.jpg", "same"), node("y/2.jpg", "same")]
    plan = build_plan(local, remote, local_root="/pics", dest_root="Photos")
    assert sorted(plan[0].remote_matches) == ["x/1.jpg", "y/2.jpg"]


def test_exif_match_is_review_not_skip():
    local = [node("a.jpg", "sig-a", exif_key="canon|r5|2024")]
    remote = [node("Photos/b.jpg", "sig-b", exif_key="canon|r5|2024")]
    plan = build_plan(local, remote, local_root="/pics", dest_root="Photos")
    assert plan[0].action == "review"
    assert plan[0].remote_matches == ("Photos/b.jpg",)
    assert "EXIF" in plan[0].reason


def test_phash_match_is_review_within_same_provenance():
    local = [node("a.jpg", "sig-a", phash="0000000000000000", phash_src="thumb")]
    remote = [node("Photos/b.jpg", "sig-b", phash="0000000000000003",
                   phash_src="thumb")]
    plan = build_plan(local, remote, local_root="/pics", dest_root="Photos")
    assert plan[0].action == "review"


def test_phash_across_provenance_is_ignored():
    local = [node("a.jpg", "sig-a", phash="0000000000000000", phash_src="thumb")]
    remote = [node("Photos/b.jpg", "sig-b", phash="0000000000000000",
                   phash_src="full")]
    plan = build_plan(local, remote, local_root="/pics", dest_root="Photos")
    assert plan[0].action == "upload"


def test_exact_match_wins_over_variant_match():
    local = [node("a.jpg", "same", exif_key="k")]
    remote = [node("x.jpg", "same", exif_key="k"), node("y.jpg", "other", exif_key="k")]
    plan = build_plan(local, remote, local_root="/pics", dest_root="Photos")
    assert plan[0].action == "skip"


def test_plan_round_trips_through_json(tmp_path):
    entries = build_plan([node("a.jpg", "sig-a")], [], local_root="/pics",
                         dest_root="Photos")
    out = tmp_path / "plan.json"
    write_plan(entries, out, local_root="/pics", dest_root="Photos",
               remote="mega")
    loaded, header = read_plan(out)
    assert loaded == entries
    assert header["local_root"] == "/pics"
    assert header["remote"] == "mega"
    assert json.loads(out.read_text())["entries"][0]["action"] == "upload"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_planner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'megaclean.planner'`

- [ ] **Step 3: Write the implementation**

```python
# megaclean/planner.py
"""Decide what to upload. Produces a file a human reads before anything moves."""
from __future__ import annotations

import datetime as _dt
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from .dupes import BKTree, Node
from .remote import join_remote_path

PLAN_VERSION = 1


@dataclass(frozen=True)
class PlanEntry:
    local_path: str
    size: int
    sig: str
    action: str                      # "upload" | "skip" | "review"
    dest_path: str
    remote_matches: tuple[str, ...]
    reason: str


def build_plan(local_nodes: Sequence[Node], remote_nodes: Sequence[Node], *,
               local_root: str, dest_root: str,
               phash_threshold: int = 6) -> list[PlanEntry]:
    by_sig: dict[str, list[str]] = defaultdict(list)
    by_exif: dict[str, list[str]] = defaultdict(list)
    for n in remote_nodes:
        if n.sig:
            by_sig[n.sig].append(n.path)
        if n.exif_key:
            by_exif[n.exif_key].append(n.path)

    trees: dict[str, BKTree] = {}
    owners: dict[str, dict[str, list[str]]] = {}
    for src in ("thumb", "full"):
        members = [n for n in remote_nodes if n.phash and n.phash_src == src]
        if not members:
            continue
        tree, owner = BKTree(), defaultdict(list)
        for n in members:
            tree.add(n.phash)
            owner[n.phash].append(n.path)
        trees[src], owners[src] = tree, owner

    entries = []
    for n in sorted(local_nodes, key=lambda x: x.path):
        dest = join_remote_path(dest_root, n.path)
        exact = sorted(by_sig.get(n.sig, []))
        if exact:
            entries.append(PlanEntry(
                local_path=n.path, size=n.size, sig=n.sig, action="skip",
                dest_path=dest, remote_matches=tuple(exact),
                reason="identical content already in the account",
            ))
            continue

        matches: list[str] = []
        reasons: list[str] = []
        if n.exif_key and by_exif.get(n.exif_key):
            matches.extend(by_exif[n.exif_key])
            reasons.append("EXIF key matches a remote photo")
        if n.phash and n.phash_src in trees:
            for neighbour in trees[n.phash_src].query(n.phash, phash_threshold):
                matches.extend(owners[n.phash_src][neighbour])
            if matches and "perceptual" not in " ".join(reasons):
                reasons.append("perceptual hash is close to a remote photo")

        if matches:
            # Deliberately not a skip: a variant match is not proof of presence.
            entries.append(PlanEntry(
                local_path=n.path, size=n.size, sig=n.sig, action="review",
                dest_path=dest, remote_matches=tuple(sorted(set(matches))),
                reason="; ".join(reasons),
            ))
            continue

        entries.append(PlanEntry(
            local_path=n.path, size=n.size, sig=n.sig, action="upload",
            dest_path=dest, remote_matches=(), reason="not found in the account",
        ))
    return entries


def write_plan(entries: Sequence[PlanEntry], path: Path, *, local_root: str,
               dest_root: str, remote: str) -> None:
    payload = {
        "plan_version": PLAN_VERSION,
        "generated": _dt.datetime.now().isoformat(timespec="seconds"),
        "local_root": local_root,
        "dest_root": dest_root,
        "remote": remote,
        "entries": [
            {**asdict(e), "remote_matches": list(e.remote_matches)}
            for e in entries
        ],
    }
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_plan(path: Path) -> tuple[list[PlanEntry], dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("plan_version") != PLAN_VERSION:
        raise ValueError(
            f"unsupported plan_version {payload.get('plan_version')!r}; "
            f"regenerate with plan-upload"
        )
    entries = [
        PlanEntry(**{**e, "remote_matches": tuple(e["remote_matches"])})
        for e in payload["entries"]
    ]
    header = {k: v for k, v in payload.items() if k != "entries"}
    return entries, header
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_planner.py -v`
Expected: all 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add megaclean/planner.py tests/test_planner.py
git commit -m "feat: upload planner with skip/review/upload decisions"
```

---

### Task 11: Upload executor

**Files:**
- Create: `megaclean/uploader.py`
- Test: `tests/test_uploader.py`

**Interfaces:**
- Consumes: `PlanEntry` (Task 10), `Remote` (Task 5), index writers (Task 1).
- Produces:
  - `@dataclass UploadResult: uploaded: int; skipped: int; failed: int; errors: list[tuple[str, str]]`
  - `execute_plan(conn, remote: Remote, entries: Sequence[PlanEntry], *, local_root: Path, scope: str, include_review: bool = False, dry_run: bool = False, on_progress=None) -> UploadResult`

**Critical detail:** `review` entries are uploaded only when `include_review=True` is passed explicitly. The executor decides nothing else — it does exactly what the plan file says.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_uploader.py
from pathlib import Path

import pytest
from megaclean.index import open_index, iter_nodes
from megaclean.planner import PlanEntry
from megaclean.remote import FakeRemote, RemoteError
from megaclean.uploader import execute_plan


def entry(local_path, action="upload", dest=None):
    return PlanEntry(local_path=local_path, size=3, sig="sig-" + local_path,
                     action=action, dest_path=dest or f"Photos/{local_path}",
                     remote_matches=(), reason="")


@pytest.fixture
def root(tmp_path, make_jpeg):
    make_jpeg(tmp_path / "a.jpg")
    make_jpeg(tmp_path / "b.jpg")
    return tmp_path


def test_uploads_only_upload_actions(root, tmp_path):
    remote, conn = FakeRemote({}), open_index(tmp_path / "i.db")
    result = execute_plan(conn, remote, [entry("a.jpg"), entry("b.jpg", "skip")],
                          local_root=root, scope="remote")
    assert (result.uploaded, result.skipped, result.failed) == (1, 1, 0)
    assert remote.uploads == [(str(root / "a.jpg"), "Photos/a.jpg")]


def test_review_entries_are_skipped_by_default(root, tmp_path):
    remote, conn = FakeRemote({}), open_index(tmp_path / "i.db")
    result = execute_plan(conn, remote, [entry("a.jpg", "review")],
                          local_root=root, scope="remote")
    assert result.uploaded == 0 and result.skipped == 1
    assert remote.uploads == []


def test_review_entries_upload_when_explicitly_included(root, tmp_path):
    remote, conn = FakeRemote({}), open_index(tmp_path / "i.db")
    result = execute_plan(conn, remote, [entry("a.jpg", "review")],
                          local_root=root, scope="remote", include_review=True)
    assert result.uploaded == 1
    assert remote.uploads == [(str(root / "a.jpg"), "Photos/a.jpg")]


def test_dry_run_transfers_nothing(root, tmp_path):
    remote, conn = FakeRemote({}), open_index(tmp_path / "i.db")
    result = execute_plan(conn, remote, [entry("a.jpg")], local_root=root,
                          scope="remote", dry_run=True)
    assert result.uploaded == 1
    assert remote.uploads == []


def test_successful_upload_lands_in_the_remote_index(root, tmp_path):
    remote, conn = FakeRemote({}), open_index(tmp_path / "i.db")
    execute_plan(conn, remote, [entry("a.jpg")], local_root=root, scope="remote")
    rows = {r["path"]: r for r in iter_nodes(conn, "remote")}
    assert "Photos/a.jpg" in rows
    assert rows["Photos/a.jpg"]["sig"] == "sig-a.jpg"


def test_failure_is_collected_and_the_run_continues(root, tmp_path):
    class Failing(FakeRemote):
        def upload(self, local_path, dest_path):
            if "a.jpg" in local_path:
                raise RemoteError("quota exceeded")
            return super().upload(local_path, dest_path)

    remote, conn = Failing({}), open_index(tmp_path / "i.db")
    result = execute_plan(conn, remote, [entry("a.jpg"), entry("b.jpg")],
                          local_root=root, scope="remote")
    assert (result.uploaded, result.failed) == (1, 1)
    assert result.errors[0][0] == "a.jpg"
    assert "quota exceeded" in result.errors[0][1]


def test_missing_local_file_is_an_error_not_a_crash(root, tmp_path):
    remote, conn = FakeRemote({}), open_index(tmp_path / "i.db")
    result = execute_plan(conn, remote, [entry("gone.jpg")], local_root=root,
                          scope="remote")
    assert result.failed == 1
    assert remote.uploads == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_uploader.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'megaclean.uploader'`

- [ ] **Step 3: Write the implementation**

```python
# megaclean/uploader.py
"""Execute an approved plan. Creates only; nothing here removes anything."""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .index import set_signature, upsert_node
from .planner import PlanEntry
from .remote import Remote

log = logging.getLogger(__name__)


@dataclass
class UploadResult:
    uploaded: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


def execute_plan(conn, remote: Remote, entries: Sequence[PlanEntry], *,
                 local_root: Path, scope: str, include_review: bool = False,
                 dry_run: bool = False,
                 on_progress: Callable[[PlanEntry, str], None] | None = None
                 ) -> UploadResult:
    """Do exactly what the plan says.

    `review` entries carry a possible-variant match and are left alone unless
    the caller explicitly opts into them.
    """
    local_root = Path(local_root)
    result = UploadResult()
    wanted = {"upload"} | ({"review"} if include_review else set())

    for entry in entries:
        if entry.action not in wanted:
            result.skipped += 1
            if on_progress:
                on_progress(entry, "skipped")
            continue

        source = local_root / entry.local_path
        try:
            if not source.is_file():
                raise FileNotFoundError(f"{source} is gone since the plan was made")
            if not dry_run:
                remote.upload(str(source), entry.dest_path)
                upsert_node(conn, scope, entry.dest_path, entry.size, "")
                set_signature(conn, scope, entry.dest_path, sig=entry.sig,
                              exif_key=None, phash=None, phash_src=None,
                              width=None, height=None)
            result.uploaded += 1
            if on_progress:
                on_progress(entry, "uploaded")
        except Exception as exc:
            result.failed += 1
            result.errors.append((entry.local_path, str(exc)))
            log.warning("upload failed for %s: %s", entry.local_path, exc)
            if on_progress:
                on_progress(entry, "failed")
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_uploader.py -v`
Expected: all 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add megaclean/uploader.py tests/test_uploader.py
git commit -m "feat: plan executor that uploads only what the plan approves"
```

---

### Task 12: Command-line interface

**Files:**
- Create: `megaclean/cli.py`
- Modify: `megaclean/dupes.py` (add `nodes_from_rows`)
- Modify: `tests/test_dupes.py` (add its test)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: every module from Tasks 1-11.
- Produces:
  - `nodes_from_rows(rows: Iterable[Mapping]) -> list[Node]` in `dupes.py` — skips rows with no `sig`.
  - `main(argv: list[str] | None = None, *, remote_factory: Callable[[str], Remote] = RcloneRemote) -> int` in `cli.py`.
  - Subcommands: `scan-remote`, `fingerprint`, `dupes`, `scan-local`, `plan-upload`, `upload`.

**Critical detail:** `remote_factory` is the seam that lets every CLI flow be tested end to end against `FakeRemote` with no network and no account.

- [ ] **Step 1: Write the failing test for `nodes_from_rows`**

```python
# append to tests/test_dupes.py
from megaclean.dupes import nodes_from_rows


def test_nodes_from_rows_skips_unfingerprinted():
    rows = [
        {"path": "a.jpg", "size": 10, "sig": "s", "exif_key": None,
         "phash": None, "phash_src": None, "width": None, "height": None,
         "mtime": "t"},
        {"path": "b.jpg", "size": 10, "sig": None, "exif_key": None,
         "phash": None, "phash_src": None, "width": None, "height": None,
         "mtime": "t"},
    ]
    nodes = nodes_from_rows(rows)
    assert [n.path for n in nodes] == ["a.jpg"]
```

- [ ] **Step 2: Add `nodes_from_rows` to `megaclean/dupes.py`**

```python
from collections.abc import Iterable, Mapping


def nodes_from_rows(rows: Iterable[Mapping]) -> list[Node]:
    """Build Nodes from index rows, dropping anything not yet fingerprinted."""
    nodes = []
    for row in rows:
        if not row["sig"]:
            continue
        nodes.append(Node(
            path=row["path"], size=row["size"], sig=row["sig"],
            exif_key=row["exif_key"], phash=row["phash"],
            phash_src=row["phash_src"], width=row["width"],
            height=row["height"], mtime=row["mtime"] or "",
        ))
    return nodes
```

Run: `pytest tests/test_dupes.py -v` — expected PASS.

- [ ] **Step 3: Write the failing CLI test**

```python
# tests/test_cli.py
import json
from pathlib import Path

import pytest
from megaclean.cli import main
from megaclean.index import open_index, iter_nodes
from megaclean.remote import FakeRemote


@pytest.fixture
def account(make_jpeg, tmp_path):
    """A fake MEGA account holding one photo plus a byte-identical copy."""
    src = tmp_path / "src"
    a = make_jpeg(src / "a.jpg", size=(200, 200))
    data = a.read_bytes()
    return FakeRemote({
        "Photos/a.jpg": data,
        "Photos/backup/a-copy.jpg": data,
        # A distinct pixel size guarantees a distinct byte size, so this
        # file is genuinely outside the collision set.
        "Photos/other.jpg": make_jpeg(src / "o.jpg", size=(640, 480),
                                      color=(0, 255, 0),
                                      dt="2024:09:09 09:09:09").read_bytes(),
    })


@pytest.fixture
def factory(account):
    return lambda name: account


def test_scan_remote_populates_the_index(tmp_path, factory):
    db = tmp_path / "i.db"
    assert main(["--db", str(db), "scan-remote", "--remote", "mega",
                 "--root", "Photos"], remote_factory=factory) == 0
    paths = [r["path"] for r in iter_nodes(open_index(db), "remote")]
    assert paths == ["Photos/a.jpg", "Photos/backup/a-copy.jpg",
                     "Photos/other.jpg"]


def test_fingerprint_collisions_scope_covers_only_size_collisions(tmp_path, factory):
    db = tmp_path / "i.db"
    main(["--db", str(db), "scan-remote", "--remote", "mega", "--root",
          "Photos"], remote_factory=factory)
    main(["--db", str(db), "fingerprint", "--remote", "mega", "--root",
          "Photos", "--scope", "collisions"], remote_factory=factory)
    rows = {r["path"]: r for r in iter_nodes(open_index(db), "remote")}
    assert rows["Photos/a.jpg"]["sig"] == rows["Photos/backup/a-copy.jpg"]["sig"]
    assert rows["Photos/other.jpg"]["sig"] is None   # unique size, never fetched


def test_full_scope_fingerprints_everything(tmp_path, factory):
    db = tmp_path / "i.db"
    main(["--db", str(db), "scan-remote", "--remote", "mega", "--root",
          "Photos"], remote_factory=factory)
    main(["--db", str(db), "fingerprint", "--remote", "mega", "--root",
          "Photos", "--scope", "all"], remote_factory=factory)
    rows = {r["path"]: r for r in iter_nodes(open_index(db), "remote")}
    assert all(r["sig"] for r in rows.values())


def test_dupes_writes_a_report(tmp_path, factory, capsys):
    db = tmp_path / "i.db"
    main(["--db", str(db), "scan-remote", "--remote", "mega", "--root",
          "Photos"], remote_factory=factory)
    main(["--db", str(db), "fingerprint", "--remote", "mega", "--root",
          "Photos", "--scope", "all"], remote_factory=factory)
    out = tmp_path / "r.html"
    assert main(["--db", str(db), "dupes", "--out", str(out),
                 "--csv", str(tmp_path / "r.csv")], remote_factory=factory) == 0
    html = out.read_text()
    assert "Photos/a.jpg" in html and "Photos/backup/a-copy.jpg" in html
    assert (tmp_path / "r.csv").exists()


def test_plan_upload_skips_content_already_present(tmp_path, factory, make_jpeg,
                                                   account):
    db = tmp_path / "i.db"
    local = tmp_path / "local"
    (local).mkdir()
    (local / "already.jpg").write_bytes(account.files["Photos/a.jpg"])
    make_jpeg(local / "brand-new.jpg", size=(123, 91), dt="2025:01:01 00:00:00")

    main(["--db", str(db), "scan-remote", "--remote", "mega", "--root",
          "Photos"], remote_factory=factory)
    main(["--db", str(db), "fingerprint", "--remote", "mega", "--root",
          "Photos", "--scope", "all"], remote_factory=factory)
    main(["--db", str(db), "scan-local", str(local)], remote_factory=factory)

    plan = tmp_path / "plan.json"
    assert main(["--db", str(db), "plan-upload", str(local), "--dest-root",
                 "Photos", "--out", str(plan)], remote_factory=factory) == 0
    actions = {e["local_path"]: e["action"]
               for e in json.loads(plan.read_text())["entries"]}
    assert actions["already.jpg"] == "skip"
    assert actions["brand-new.jpg"] == "upload"


def test_upload_executes_the_plan(tmp_path, factory, make_jpeg, account):
    db = tmp_path / "i.db"
    local = tmp_path / "local"
    local.mkdir()
    make_jpeg(local / "brand-new.jpg", size=(123, 91), dt="2025:01:01 00:00:00")
    main(["--db", str(db), "scan-remote", "--remote", "mega", "--root",
          "Photos"], remote_factory=factory)
    main(["--db", str(db), "fingerprint", "--remote", "mega", "--root",
          "Photos", "--scope", "all"], remote_factory=factory)
    main(["--db", str(db), "scan-local", str(local)], remote_factory=factory)
    plan = tmp_path / "plan.json"
    main(["--db", str(db), "plan-upload", str(local), "--dest-root", "Photos",
          "--out", str(plan)], remote_factory=factory)
    assert main(["--db", str(db), "upload", "--plan", str(plan), "--remote",
                 "mega"], remote_factory=factory) == 0
    assert "Photos/brand-new.jpg" in account.files


def test_upload_dry_run_transfers_nothing(tmp_path, factory, make_jpeg, account):
    db = tmp_path / "i.db"
    local = tmp_path / "local"
    local.mkdir()
    make_jpeg(local / "brand-new.jpg", size=(123, 91), dt="2025:01:01 00:00:00")
    main(["--db", str(db), "scan-local", str(local)], remote_factory=factory)
    plan = tmp_path / "plan.json"
    main(["--db", str(db), "plan-upload", str(local), "--dest-root", "Photos",
          "--out", str(plan)], remote_factory=factory)
    main(["--db", str(db), "upload", "--plan", str(plan), "--remote", "mega",
          "--dry-run"], remote_factory=factory)
    assert account.uploads == []


def test_unknown_command_exits_nonzero(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(tmp_path / "i.db"), "nonsense"])
    assert exc.value.code != 0
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'megaclean.cli'`

- [ ] **Step 5: Write the implementation**

```python
# megaclean/cli.py
"""Command-line entry point.

`remote_factory` is injectable so every flow below is exercised in tests
against an in-memory account.
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path

from . import __version__
from .dupes import cluster_nodes, nodes_from_rows
from .fingerprint import run_fingerprint
from .index import (
    clear_errors, iter_nodes, open_index, pending_paths, size_collision_paths,
    upsert_node,
)
from .local import local_scope, scan_local
from .planner import build_plan, read_plan, write_plan
from .remote import RcloneRemote, Remote, join_remote_path
from .report import summarize, write_csv, write_html
from .uploader import execute_plan

DEFAULT_DB = ".megaclean/index.db"


def _cmd_scan_remote(args, conn, make_remote) -> int:
    remote = make_remote(args.remote)
    count = 0
    for f in remote.list_files(args.root):
        # Store the full account path: read_range and upload address the
        # account, so a root-relative path would resolve to nothing.
        upsert_node(conn, "remote", join_remote_path(args.root, f.path),
                    f.size, f.mtime)
        count += 1
    print(f"indexed {count} remote files under {args.remote}:{args.root}")
    return 0


def _cmd_fingerprint(args, conn, make_remote) -> int:
    if args.retry:
        print(f"cleared {clear_errors(conn, 'remote')} previous errors")
    sizes = {r["path"]: r["size"] for r in iter_nodes(conn, "remote")}
    if not sizes:
        print("nothing indexed yet — run scan-remote first", file=sys.stderr)
        return 1
    if args.scope == "collisions":
        candidates = size_collision_paths(conn, "remote")
    else:
        candidates = list(sizes)
    paths = pending_paths(conn, "remote", candidates)
    if not paths:
        print("nothing pending")
        return 0
    print(f"fingerprinting {len(paths)} files (scope={args.scope}, "
          f"workers={args.workers})")
    ok, failed = run_fingerprint(conn, make_remote(args.remote), "remote",
                                 paths, workers=args.workers, sizes=sizes)
    print(f"fingerprinted {ok}, failed {failed}")
    return 0 if failed == 0 else 1


def _cmd_dupes(args, conn, make_remote) -> int:
    nodes = nodes_from_rows(iter_nodes(conn, "remote"))
    if not nodes:
        print("no fingerprinted files — run fingerprint first", file=sys.stderr)
        return 1
    clusters = cluster_nodes(nodes, phash_threshold=args.threshold)
    write_html(clusters, Path(args.out))
    if args.csv:
        write_csv(clusters, Path(args.csv))
    stats = summarize(clusters)
    print(f"{stats['clusters']} clusters "
          f"({stats['exact_clusters']} exact, {stats['variant_clusters']} variant), "
          f"{stats['redundant_files']} redundant files, "
          f"{stats['redundant_bytes'] / 1e9:.2f} GB reclaimable")
    print(f"report written to {args.out}")
    return 0


def _cmd_scan_local(args, conn, make_remote) -> int:
    root = Path(args.path).resolve()
    scanned, failed = scan_local(conn, root, include_video=args.include_video)
    print(f"scanned {scanned} local files, {failed} unreadable")
    return 0


def _cmd_plan_upload(args, conn, make_remote) -> int:
    root = Path(args.path).resolve()
    local_nodes = nodes_from_rows(iter_nodes(conn, local_scope(root)))
    if not local_nodes:
        print(f"no local files indexed for {root} — run scan-local first",
              file=sys.stderr)
        return 1
    remote_nodes = nodes_from_rows(iter_nodes(conn, "remote"))
    entries = build_plan(local_nodes, remote_nodes, local_root=str(root),
                         dest_root=args.dest_root,
                         phash_threshold=args.threshold)
    write_plan(entries, Path(args.out), local_root=str(root),
               dest_root=args.dest_root, remote=args.remote)
    counts = {a: sum(1 for e in entries if e.action == a)
              for a in ("upload", "skip", "review")}
    to_upload = sum(e.size for e in entries if e.action == "upload")
    print(f"{counts['upload']} to upload ({to_upload / 1e9:.2f} GB), "
          f"{counts['skip']} already present, {counts['review']} to review")
    print(f"plan written to {args.out} — review it, then run: "
          f"megaclean upload --plan {args.out}")
    return 0


def _cmd_upload(args, conn, make_remote) -> int:
    entries, header = read_plan(Path(args.plan))
    remote_name = args.remote or header.get("remote")
    result = execute_plan(
        conn, make_remote(remote_name), entries,
        local_root=Path(header["local_root"]), scope="remote",
        include_review=args.include_review, dry_run=args.dry_run,
    )
    verb = "would upload" if args.dry_run else "uploaded"
    print(f"{verb} {result.uploaded}, skipped {result.skipped}, "
          f"failed {result.failed}")
    for path, error in result.errors[:20]:
        print(f"  {path}: {error}", file=sys.stderr)
    return 0 if result.failed == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="megaclean",
        description="Review duplicates in a MEGA photo library and upload "
                    "only what is missing. This tool never deletes anything.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--db", default=DEFAULT_DB, help="index database path")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan-remote", help="index the account (free)")
    p.add_argument("--remote", default="mega", help="rclone remote name")
    p.add_argument("--root", default="", help="path within the remote")
    p.set_defaults(func=_cmd_scan_remote)

    p = sub.add_parser("fingerprint", help="fetch bytes needed for identity")
    p.add_argument("--remote", default="mega")
    p.add_argument("--root", default="")
    p.add_argument("--scope", choices=("collisions", "all"),
                   default="collisions",
                   help="collisions: only size-colliding files (cheap, exact "
                        "duplicates). all: every file (enables variant detection)")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--retry", action="store_true",
                   help="retry files that previously errored")
    p.set_defaults(func=_cmd_fingerprint)

    p = sub.add_parser("dupes", help="write the duplicate report")
    p.add_argument("--out", default="report.html")
    p.add_argument("--csv", default=None)
    p.add_argument("--threshold", type=int, default=6,
                   help="perceptual hash Hamming distance")
    p.set_defaults(func=_cmd_dupes)

    p = sub.add_parser("scan-local", help="index a local folder")
    p.add_argument("path")
    p.add_argument("--include-video", action="store_true")
    p.set_defaults(func=_cmd_scan_local)

    p = sub.add_parser("plan-upload", help="decide what is missing")
    p.add_argument("path")
    p.add_argument("--dest-root", default="Photos")
    p.add_argument("--out", default="plan.json")
    p.add_argument("--remote", default="mega")
    p.add_argument("--threshold", type=int, default=6)
    p.set_defaults(func=_cmd_plan_upload)

    p = sub.add_parser("upload", help="execute an approved plan")
    p.add_argument("--plan", required=True)
    p.add_argument("--remote", default=None)
    p.add_argument("--include-review", action="store_true",
                   help="also upload entries flagged as possible variants")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_cmd_upload)
    return parser


def main(argv: list[str] | None = None, *,
         remote_factory: Callable[[str], Remote] = RcloneRemote) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    conn = open_index(args.db)
    try:
        return args.func(args, conn, remote_factory)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run the full suite**

Run: `pytest -v`
Expected: every test PASSES

- [ ] **Step 7: Commit**

```bash
git add megaclean/cli.py megaclean/dupes.py tests/test_cli.py tests/test_dupes.py
git commit -m "feat: CLI wiring all commands with an injectable remote"
```

---

### Task 13: `doctor` and documentation

**Files:**
- Create: `megaclean/doctor.py`
- Modify: `megaclean/cli.py` (register the `doctor` subcommand)
- Create: `README.md`
- Create: `tests/test_doctor.py`
- Create: `tests/test_integration.py`

**Interfaces:**
- Consumes: `Remote` (Task 5), index readers (Task 1).
- Produces:
  - `@dataclass(frozen=True) Check: name: str; ok: bool; detail: str`
  - `check_rclone(runner=subprocess.run, binary="rclone") -> Check`
  - `check_remote_configured(remote: str, runner=subprocess.run, binary="rclone") -> Check`
  - `check_range_reads(remote: Remote, samples: list[tuple[str, int]], clock=time.monotonic) -> Check`
  - `run_checks(remote_name: str, remote: Remote, samples, runner=subprocess.run) -> list[Check]`

**Critical detail — this is the assumption the whole design rests on.** `check_range_reads` verifies that `rclone cat --offset` transfers only the requested range rather than streaming the whole file. It times a 64 KB head read of the smallest and the largest indexed file: if partial reads work, the two take similar time regardless of file size; if rclone is streaming whole files, the large one takes far longer. The spec's fallback if this fails is full content fetch restricted to size-collision groups.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_doctor.py
import pytest
from megaclean.doctor import (
    Check, check_range_reads, check_rclone, check_remote_configured,
)
from megaclean.remote import FakeRemote, RemoteError


class Runner:
    def __init__(self, stdout=b"", returncode=0):
        self.stdout, self.returncode, self.stderr = stdout, returncode, b""

    def __call__(self, argv, **kw):
        class R:
            pass
        r = R()
        r.stdout, r.stderr, r.returncode = self.stdout, self.stderr, self.returncode
        return r


def test_rclone_present_reports_version():
    check = check_rclone(runner=Runner(b"rclone v1.74.4\n- os/version: darwin\n"))
    assert check.ok
    assert "v1.74.4" in check.detail


def test_rclone_missing_is_reported_not_raised():
    def boom(argv, **kw):
        raise FileNotFoundError("rclone")
    check = check_rclone(runner=boom)
    assert not check.ok
    assert "not found" in check.detail.lower()


def test_remote_configured_detects_a_missing_remote():
    runner = Runner(b"gdrive:\n")
    assert not check_remote_configured("mega", runner=runner).ok
    assert check_remote_configured("gdrive", runner=runner).ok


def test_range_reads_pass_when_time_is_flat():
    ticks = iter([0.0, 0.5, 1.0, 1.5])
    remote = FakeRemote({"small.jpg": b"x" * 1000, "big.jpg": b"y" * 10_000_000})
    check = check_range_reads(remote, [("small.jpg", 1000), ("big.jpg", 10_000_000)],
                              clock=lambda: next(ticks))
    assert check.ok


def test_range_reads_fail_when_time_scales_with_file_size():
    ticks = iter([0.0, 0.5, 1.0, 60.0])
    remote = FakeRemote({"small.jpg": b"x" * 1000, "big.jpg": b"y" * 10_000_000})
    check = check_range_reads(remote, [("small.jpg", 1000), ("big.jpg", 10_000_000)],
                              clock=lambda: next(ticks))
    assert not check.ok
    assert "whole file" in check.detail


def test_range_reads_need_two_samples():
    check = check_range_reads(FakeRemote({}), [])
    assert not check.ok
    assert "scan-remote" in check.detail


def test_range_read_error_is_reported():
    check = check_range_reads(FakeRemote({}), [("gone.jpg", 1), ("also.jpg", 2)])
    assert not check.ok
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_doctor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'megaclean.doctor'`

- [ ] **Step 3: Write the implementation**

```python
# megaclean/doctor.py
"""Environment checks, including the one assumption the design rests on."""
from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .remote import Remote
from .signature import HEAD_BYTES

# If partial reads work, head-read time is roughly independent of file size.
# These bounds are deliberately loose: this is meant to catch whole-file
# streaming, not to benchmark a link.
SLOWDOWN_FACTOR = 4.0
SLOWDOWN_GRACE_SECONDS = 2.0


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def check_rclone(runner: Callable = subprocess.run,
                 binary: str = "rclone") -> Check:
    try:
        result = runner([binary, "version"], capture_output=True)
    except FileNotFoundError:
        return Check("rclone", False,
                     "rclone not found on PATH — install it with "
                     "`brew install rclone`")
    if result.returncode != 0:
        return Check("rclone", False, "`rclone version` exited nonzero")
    first = (result.stdout or b"").decode("utf-8", "replace").splitlines()
    return Check("rclone", True, first[0] if first else "present")


def check_remote_configured(remote: str, runner: Callable = subprocess.run,
                            binary: str = "rclone") -> Check:
    try:
        result = runner([binary, "listremotes"], capture_output=True)
    except FileNotFoundError:
        return Check("remote", False, "rclone not found on PATH")
    names = (result.stdout or b"").decode("utf-8", "replace").split()
    configured = [n.rstrip(":") for n in names]
    if remote.rstrip(":") in configured:
        return Check("remote", True, f"{remote}: configured")
    return Check("remote", False,
                 f"no rclone remote named {remote!r}; configured: "
                 f"{', '.join(configured) or 'none'}. Run `rclone config`, or "
                 f"log in with MEGAcmd first if the account uses 2FA")


def check_range_reads(remote: Remote, samples: Sequence[tuple[str, int]],
                      clock: Callable[[], float] = time.monotonic) -> Check:
    """Verify partial reads transfer only the requested range.

    The whole tiered design assumes this. If it fails, the fallback is fetching
    full content for size-collision groups only, and variant detection becomes
    impractical.
    """
    if len(samples) < 2:
        return Check("range reads", False,
                     "not enough indexed files to test — run scan-remote first")
    ordered = sorted(samples, key=lambda s: s[1])
    (small_path, small_size), (big_path, big_size) = ordered[0], ordered[-1]
    try:
        start = clock()
        remote.read_range(small_path, 0, HEAD_BYTES)
        small_elapsed = clock() - start
        start = clock()
        remote.read_range(big_path, 0, HEAD_BYTES)
        big_elapsed = clock() - start
    except Exception as exc:
        return Check("range reads", False, f"range read failed: {exc}")

    budget = small_elapsed * SLOWDOWN_FACTOR + SLOWDOWN_GRACE_SECONDS
    if big_elapsed > budget:
        return Check("range reads", False,
                     f"reading 64 KB from a {big_size / 1e6:.0f} MB file took "
                     f"{big_elapsed:.1f}s versus {small_elapsed:.1f}s from a "
                     f"{small_size / 1e6:.1f} MB file — rclone appears to be "
                     f"downloading the whole file. Use --scope collisions only "
                     f"and expect full-file transfer costs")
    return Check("range reads", True,
                 f"partial reads look genuine ({small_elapsed:.2f}s vs "
                 f"{big_elapsed:.2f}s for 64 KB)")


def run_checks(remote_name: str, remote: Remote,
               samples: Sequence[tuple[str, int]],
               runner: Callable = subprocess.run) -> list[Check]:
    checks = [check_rclone(runner=runner),
              check_remote_configured(remote_name, runner=runner)]
    if checks[1].ok:
        checks.append(check_range_reads(remote, samples))
    return checks
```

- [ ] **Step 4: Register the subcommand in `megaclean/cli.py`**

Add the import beside the other module imports:

```python
from .doctor import run_checks
```

Add the handler beside the other `_cmd_*` functions:

```python
def _cmd_doctor(args, conn, make_remote) -> int:
    samples = [(r["path"], r["size"]) for r in iter_nodes(conn, "remote")]
    checks = run_checks(args.remote, make_remote(args.remote), samples)
    for check in checks:
        print(f"[{'ok' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
    return 0 if all(c.ok for c in checks) else 1
```

Register it in `build_parser`, immediately before `return parser`:

```python
    p = sub.add_parser("doctor", help="verify the environment and assumptions")
    p.add_argument("--remote", default="mega")
    p.set_defaults(func=_cmd_doctor)
```

- [ ] **Step 5: Write the opt-in integration test**

```python
# tests/test_integration.py
"""Opt-in checks against a real MEGA account.

Run with: MEGACLEAN_TEST_REMOTE=mega MEGACLEAN_TEST_ROOT=Photos \
          pytest -m integration
"""
import os

import pytest
from megaclean.doctor import check_range_reads, check_remote_configured
from megaclean.remote import RcloneRemote

pytestmark = pytest.mark.integration

REMOTE = os.environ.get("MEGACLEAN_TEST_REMOTE", "")
ROOT = os.environ.get("MEGACLEAN_TEST_ROOT", "")


@pytest.fixture
def remote():
    if not REMOTE:
        pytest.skip("set MEGACLEAN_TEST_REMOTE to run integration tests")
    return RcloneRemote(REMOTE)


def test_remote_is_configured():
    if not REMOTE:
        pytest.skip("set MEGACLEAN_TEST_REMOTE to run integration tests")
    assert check_remote_configured(REMOTE).ok


def test_listing_returns_files(remote):
    files = list(remote.list_files(ROOT))
    assert files, f"no files found under {REMOTE}:{ROOT}"


def test_partial_reads_are_genuinely_partial(remote):
    samples = [(f.path, f.size) for f in remote.list_files(ROOT)]
    check = check_range_reads(remote, samples)
    assert check.ok, check.detail


def test_head_read_returns_the_requested_length(remote):
    from megaclean.signature import HEAD_BYTES
    big = max(remote.list_files(ROOT), key=lambda f: f.size)
    assert big.size > HEAD_BYTES, "no file large enough to test a bounded read"
    assert len(remote.read_range(big.path, 0, HEAD_BYTES)) == HEAD_BYTES
```

- [ ] **Step 6: Write the safety guard test**

```python
# append to tests/test_doctor.py
import pathlib
import re


def test_no_deletion_capability_exists_in_the_package():
    """The no-delete property is enforced by the codebase, not by a default."""
    forbidden = re.compile(
        r"\brclone[\"']?\s*,\s*[\"']?(delete|deletefile|purge|rmdir|rmdirs)\b"
        r"|\bos\.remove\b|\bos\.unlink\b|\bshutil\.rmtree\b"
        r"|\.unlink\(|\bmega-rm\b"
    )
    package = pathlib.Path(__file__).resolve().parent.parent / "megaclean"
    offenders = [
        f.name for f in package.glob("*.py") if forbidden.search(f.read_text())
    ]
    assert offenders == [], f"deletion capability found in: {offenders}"
```

Run: `pytest tests/test_doctor.py -v` — expected PASS.

- [ ] **Step 7: Write `README.md`**

````markdown
# mega-clean

Review duplicates in a MEGA photo library and upload only the local photos that
are not already there. **This tool never deletes anything** — `dupes` writes a
report, `upload` only creates.

## Why it works the way it does

MEGA exposes no content hash. Neither "are these two remote files identical" nor
"is this local photo already up there" can be answered by asking MEGA, so
content identity is derived client-side from bytes fetched over range requests,
in tiers that get more expensive as they get more capable.

## Setup

```bash
brew install rclone
rclone config                 # add a remote named "mega"
pip install -e '.[dev]'
megaclean doctor --remote mega
```

If the account uses two-factor auth, rclone's mega backend cannot log in;
install MEGAcmd (`brew install megacmd`), authenticate there, and configure
rclone against the same account.

Run `megaclean doctor` before anything else. It verifies the assumption the
whole design rests on: that `rclone cat --offset` fetches only the range asked
for rather than streaming the whole file.

## Finding duplicates

```bash
megaclean scan-remote --remote mega --root Photos      # free: paths and sizes
megaclean fingerprint --remote mega --scope collisions # cheap: exact duplicates
megaclean dupes --out report.html --csv report.csv
```

`--scope collisions` only inspects files whose byte size collides with another
file, because nothing else can be byte-identical. That is most of the account
skipped.

To find variants — the same photo re-encoded, resized or renamed — run the full
sweep once. It reads the first 64 KB of every image for EXIF and the embedded
camera thumbnail:

```bash
megaclean fingerprint --remote mega --scope all
megaclean dupes --out report.html
```

Both are resumable: interrupt them and re-run, and they pick up what is still
pending. `--retry` re-attempts files that errored.

**Variant detection is a heuristic.** EXIF matching is the strong signal.
Perceptual hashing depends on the camera's embedded thumbnail surviving the
edit, and some tools strip it. Read the report; do not act on it blindly.

## Uploading what is missing

```bash
megaclean scan-local ~/Pictures/2024
megaclean plan-upload ~/Pictures/2024 --dest-root Photos --out plan.json
# read plan.json
megaclean upload --plan plan.json
```

Planning and uploading are separate so that the upload step decides nothing.
Every entry carries one of three actions:

- `upload` — not in the account; will be uploaded to the mirrored path.
- `skip` — byte-identical content already exists somewhere in the account, and
  the entry records every path where it lives.
- `review` — an EXIF or perceptual match, but not a byte match. **Not uploaded
  and not skipped.** You decide. `--include-review` uploads them anyway.

`--dry-run` reports what would happen and transfers nothing.

Note that `plan-upload` can only recognise a re-encoded copy already in MEGA if
`fingerprint --scope all` has been run at least once. With only the cheap scope,
a local photo is compared against remote files that share its exact byte size.

## Tests

```bash
pytest                                  # offline; no account needed
MEGACLEAN_TEST_REMOTE=mega MEGACLEAN_TEST_ROOT=Photos pytest -m integration
```
````

- [ ] **Step 8: Run the full suite**

Run: `pytest -v && megaclean --help`
Expected: every test PASSES and the CLI prints its help

- [ ] **Step 9: Commit**

```bash
git add megaclean/doctor.py megaclean/cli.py README.md tests/test_doctor.py tests/test_integration.py
git commit -m "feat: doctor command verifying partial reads, plus documentation"
```
