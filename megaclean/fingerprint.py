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
    tail = (remote.read_range(path, -TAIL_BYTES, TAIL_BYTES)
            if needs_tail_read(size) else b"")
    return fingerprint_from_parts(size, head, tail)


def _fetch_with_backoff(remote: Remote, path: str, size: int,
                        max_attempts: int) -> Fingerprint:
    """MEGA throttles aggressive clients, so back off rather than hammer it."""
    last: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fetch_fingerprint(remote, path, size)
        except RemoteError as exc:
            last = exc
            if attempt == max_attempts - 1:
                break
            time.sleep((2 ** attempt) + random.random())
    assert last is not None
    raise last


def run_fingerprint(conn, remote: Remote, scope: str, paths: list[str], *,
                    workers: int = 8, sizes: dict[str, int],
                    max_attempts: int = MAX_ATTEMPTS,
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
            pool.submit(_fetch_with_backoff, remote, p, sizes[p], max_attempts): p
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
