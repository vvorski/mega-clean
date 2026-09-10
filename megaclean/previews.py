"""Fetch real preview images for the duplicate gallery.

A 64 KB head only decodes the top few percent of a multi-megabyte photo, which
is not a usable preview. These fetch whole files -- but only one per group,
because the members of a duplicate group are the same picture, and only for the
groups worth looking at.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .dupes import Cluster
from .local import IMAGE_EXTENSIONS
from .remote import Remote
from .thumbs import thumb_path, write_thumbnail

log = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 12 * 1024 * 1024


def _wasted(cluster: Cluster) -> int:
    return sum(m.size for m in cluster.members if m.key not in cluster.keeper_keys)


def preview_targets(clusters: Sequence[Cluster], thumb_root: Path, *,
                    limit: int | None = None,
                    force: bool = False) -> list[tuple[str, str, int]]:
    """(node_id, path, size) for the images worth fetching, best groups first.

    Identical groups contribute only their keeper; variant groups contribute
    every member, since those genuinely differ.
    """
    targets: list[tuple[str, str, int]] = []
    ranked = sorted(clusters, key=lambda c: -_wasted(c))
    groups_used = 0
    for cluster in ranked:
        if limit is not None and groups_used >= limit:
            break
        members = (cluster.members if cluster.kind == "variant"
                   else (cluster.keeper,))
        picked = []
        for member in members:
            if Path(member.path).suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            if not force and thumb_path(thumb_root, member.key,
                                        large=True).is_file():
                continue
            picked.append((member.key, member.path, member.size))
        if picked:
            targets.extend(picked)
            groups_used += 1
    return targets


def fetch_previews(remote: Remote, targets: Sequence[tuple[str, str, int]],
                   thumb_root: Path, *, workers: int = 8,
                   max_bytes: int = DEFAULT_MAX_BYTES,
                   on_progress=None) -> tuple[int, int]:
    ok = failed = 0
    if not targets:
        return (0, 0)

    def fetch(target):
        node_id, path, size = target
        return node_id, remote.read_range(path, 0, min(size, max_bytes))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, t): t for t in targets}
        for future in as_completed(futures):
            target = futures[future]
            try:
                node_id, data = future.result()
                # Whole-file bytes in hand: write the click-through image too.
                if write_thumbnail(thumb_root, node_id, data, also_large=True):
                    ok += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                log.warning("preview failed for %s: %s", target[1], exc)
            if on_progress:
                on_progress(target, True)
    return ok, failed
