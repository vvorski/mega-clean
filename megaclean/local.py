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
    allowed = IMAGE_EXTENSIONS | (VIDEO_EXTENSIONS if include_video
                                  else frozenset())
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
