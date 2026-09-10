"""Move redundant copies to MEGA's Rubbish Bin, from a reviewed plan.

This is the one write the tool performs, and it is deliberately the reversible
one: the bin keeps everything until the user empties it, which this tool never
does. Two invariants make it safe to run: nothing in a plan is ever a keeper,
and by default only byte-verified copies qualify.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .dupes import Cluster, choose_keeper

log = logging.getLogger(__name__)

PLAN_VERSION = 1


@dataclass(frozen=True)
class BinEntry:
    handle: str
    path: str
    size: int
    kept_handle: str
    kept_path: str
    evidence: str          # "byte" | "fingerprint"


@dataclass
class BinResult:
    moved: int = 0
    failed: int = 0
    errors: list[tuple[str, int]] = field(default_factory=list)


def build_bin_plan(clusters: Sequence[Cluster], *,
                   only_under: Sequence[str] = (),
                   allow_unverified: bool = False) -> list[BinEntry]:
    """Every redundant copy, with the copy that survives it named alongside.

    Redundancy is decided inside byte-identical subgroups only, exactly as the
    report does, so a variant cluster never bins one burst frame against
    another.
    """
    entries: list[BinEntry] = []
    for cluster in clusters:
        for group in cluster.content_groups:
            if len(group) < 2:
                continue
            keeper = choose_keeper(list(group), cluster.prefer, cluster.avoid)
            for member in group:
                if member.key == keeper.key:
                    continue
                if only_under and not member.path.startswith(tuple(only_under)):
                    continue
                evidence = "byte" if (member.sig and keeper.sig) else "fingerprint"
                if evidence == "fingerprint" and not allow_unverified:
                    continue
                entries.append(BinEntry(
                    handle=member.key, path=member.path, size=member.size,
                    kept_handle=keeper.key, kept_path=keeper.path,
                    evidence=evidence))
    _check_invariants(entries)
    return entries


def _check_invariants(entries: Sequence[BinEntry]) -> None:
    binned = {e.handle for e in entries}
    kept = {e.kept_handle for e in entries}
    clash = binned & kept
    if clash:
        raise ValueError(f"plan would bin a keeper: {sorted(clash)[:5]}")
    if len(binned) != len(entries):
        raise ValueError("plan lists the same node more than once")


def write_bin_plan(entries: Sequence[BinEntry], path: Path, *, remote: str,
                   only_under: Sequence[str] = (),
                   prefer: Sequence[str] = ()) -> None:
    payload = {
        "plan_version": PLAN_VERSION,
        "generated": _dt.datetime.now().isoformat(timespec="seconds"),
        "remote": remote,
        "only_under": list(only_under),
        "prefer": list(prefer),
        "total_files": len(entries),
        "total_bytes": sum(e.size for e in entries),
        "entries": [asdict(e) for e in entries],
    }
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_bin_plan(path: Path) -> tuple[list[BinEntry], dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("plan_version") != PLAN_VERSION:
        raise ValueError("unsupported bin plan version; regenerate with plan-bin")
    entries = [BinEntry(**e) for e in payload["entries"]]
    header = {k: v for k, v in payload.items() if k != "entries"}
    return entries, header


def execute_bin_plan(api, entries: Sequence[BinEntry], rubbish: str, *,
                     dry_run: bool = False, batch_size: int = 100,
                     on_progress=None) -> BinResult:
    """Move each planned node into the Rubbish Bin. Invariants are re-checked
    here, not only at planning time, so an edited plan file cannot bypass them.
    """
    _check_invariants(entries)
    result = BinResult()
    if dry_run:
        result.moved = len(entries)
        return result
    handles = [e.handle for e in entries]
    by_handle = {e.handle: e for e in entries}
    codes = api.move_to_rubbish(handles, rubbish, batch_size=batch_size)
    for handle, code in codes.items():
        entry = by_handle[handle]
        if code == 0:
            result.moved += 1
        else:
            result.failed += 1
            result.errors.append((entry.path, code))
            log.warning("could not bin %s: MEGA error %s", entry.path, code)
        if on_progress:
            on_progress(entry, code == 0)
    return result
