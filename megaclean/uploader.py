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
                raise FileNotFoundError(
                    f"{source} is gone since the plan was made")
            if not dry_run:
                remote.upload(str(source), entry.dest_path)
                upsert_node(conn, scope, entry.dest_path, entry.size, "",
                            node_id=entry.dest_path)
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
