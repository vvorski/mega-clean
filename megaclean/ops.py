"""One manifest of every file operation, in execution order, for review.

The file-level report says which copy of each photo survives; the folder plan
says which folders go. This reconciles the two into a single list -- MOVE the
files that have no surviving copy, BIN the redundant copies, REMOVE folders
only once nothing is left in them -- with chains resolved to the final
survivor and name collisions surfaced rather than created.
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from .dupes import Node, cluster_nodes
from .folders import analyse_folders, merge_decisions

ORDER = {"MOVE": 0, "BIN": 1, "REMOVE_FOLDER": 2, "CONFLICT": 3}


@dataclass(frozen=True)
class Operation:
    kind: str                       # MOVE | BIN | REMOVE_FOLDER | CONFLICT
    path: str
    handle: str
    size: int = 0
    destination: str = ""           # MOVE: folder path
    destination_handle: str = ""
    survivor: str = ""              # BIN: the copy that stays
    survivor_handle: str = ""
    reason: str = ""


def _dir(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else "/"


def resolve_destination(folder: str, edges: dict[str, str]) -> str:
    """Follow source -> destination links to the folder that finally survives."""
    seen = {folder}
    while folder in edges and edges[folder] not in seen:
        folder = edges[folder]
        seen.add(folder)
    return folder


def build_operations(nodes: Sequence[Node], *, prefer: Sequence[str] = (),
                     inbox: Sequence[str] = (), min_files: int = 10,
                     min_coverage: float = 0.5,
                     phash_threshold: int = 6) -> list[Operation]:
    first = cluster_nodes(nodes, phash_threshold=phash_threshold, prefer=prefer)
    overlaps = analyse_folders(first, nodes, min_files=min_files)
    decisions = merge_decisions(overlaps, prefer, min_coverage, inbox)
    edges: dict[str, str] = {}
    for d in decisions:
        if not d.source_is_inbox:
            edges.setdefault(d.source, d.destination)   # first = largest overlap
    removed = set(edges)

    # Keepers must not live in a folder we are removing; re-cluster with that
    # constraint so the file plan and the folder plan agree.
    avoid = tuple(f + "/" for f in removed)
    clusters = cluster_nodes(nodes, phash_threshold=phash_threshold,
                             prefer=prefer, avoid=avoid)

    by_key = {n.key: n for n in nodes}
    by_dir: dict[str, list[Node]] = defaultdict(list)
    for n in nodes:
        by_dir[_dir(n.path)].append(n)
    folder_handle = {d: next((m.parent_id for m in ms if m.parent_id), "")
                     for d, ms in by_dir.items()}

    bins: list[Operation] = []
    binned: set[str] = set()
    for cluster in clusters:
        for group in cluster.content_groups:
            if len(group) < 2 or not all(m.sig for m in group):
                continue                     # only byte-verified evidence
            keeper = next(m for m in group if m.key in cluster.keeper_keys)
            for m in group:
                if m.key == keeper.key:
                    continue
                binned.add(m.key)
                bins.append(Operation(
                    "BIN", m.path, m.key, m.size, survivor=keeper.path,
                    survivor_handle=keeper.key,
                    reason="byte-identical copy survives"))

    moves: list[Operation] = []
    conflicts: list[Operation] = []
    removals: list[Operation] = []
    incoming: dict[str, set[str]] = defaultdict(set)     # dest dir -> names
    for source in edges:
        dest = resolve_destination(source, edges)
        dest_names = {m.path.rsplit("/", 1)[-1] for m in by_dir.get(dest, ())
                      if m.key not in binned}
        clean = True
        for m in by_dir.get(source, ()):
            if m.key in binned:
                continue
            name = m.path.rsplit("/", 1)[-1]
            if name in dest_names or name in incoming[dest]:
                clean = False
                conflicts.append(Operation(
                    "CONFLICT", m.path, m.key, m.size, destination=dest,
                    destination_handle=folder_handle.get(dest, ""),
                    reason=f"`{dest}` already holds a different file named "
                           f"{name}; moving would create an indistinguishable "
                           f"pair -- rename or decide by hand"))
                continue
            incoming[dest].add(name)
            moves.append(Operation(
                "MOVE", m.path, m.key, m.size, destination=dest,
                destination_handle=folder_handle.get(dest, ""),
                reason="no surviving copy elsewhere"))
        has_descendants = any(_dir(n.path) != source and
                              n.path.startswith(source + "/") for n in nodes)
        if clean and not has_descendants:
            removals.append(Operation(
                "REMOVE_FOLDER", source, folder_handle.get(source, ""),
                reason="empty once the moves and bins above are done"))

    ops = moves + bins + removals + conflicts
    ops.sort(key=lambda o: (ORDER[o.kind], o.path))
    return ops


def _human(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"


def write_operations(ops: Sequence[Operation], out_base: Path) -> None:
    """Write <base>.json (machine), <base>.csv (spreadsheet), <base>.md (human)."""
    base = Path(out_base)
    base.parent.mkdir(parents=True, exist_ok=True)
    counts = defaultdict(lambda: [0, 0])
    for o in ops:
        counts[o.kind][0] += 1
        counts[o.kind][1] += o.size

    base.with_suffix(".json").write_text(json.dumps({
        "generated": _dt.datetime.now().isoformat(timespec="seconds"),
        "counts": {k: {"files": v[0], "bytes": v[1]} for k, v in counts.items()},
        "operations": [asdict(o) for o in ops],
    }, indent=1), encoding="utf-8")

    with base.with_suffix(".csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["order", "kind", "path", "size", "destination", "survivor",
                    "handle", "reason"])
        for i, o in enumerate(ops, start=1):
            w.writerow([i, o.kind, o.path, o.size, o.destination, o.survivor,
                        o.handle, o.reason])

    lines = ["# Operations manifest", "",
             "Every operation, in the order it would run. **Nothing has been "
             "executed.** MOVEs run first, then BINs (to the Rubbish Bin, "
             "reversible), then folder removals of folders left empty. "
             "CONFLICTs are not executed at all.", ""]
    for kind in ("MOVE", "BIN", "REMOVE_FOLDER", "CONFLICT"):
        n_, b = counts.get(kind, (0, 0))
        lines.append(f"- **{kind}**: {n_:,} ({_human(b)})")
    lines.append("")

    moves = [o for o in ops if o.kind == "MOVE"]
    if moves:
        lines += ["## MOVE — files with no surviving copy, rescued first", ""]
        by_dest = defaultdict(list)
        for o in moves:
            by_dest[(_dir(o.path), o.destination)].append(o)
        for (src, dest), group in sorted(by_dest.items()):
            lines.append(f"### `{src}`  →  `{dest}`  ({len(group)} files, "
                         f"{_human(sum(o.size for o in group))})")
            lines += [f"- `{o.path}`" for o in group]
            lines.append("")

    bins = [o for o in ops if o.kind == "BIN"]
    if bins:
        lines += ["## BIN — redundant copies, by folder", "",
                  "Full per-file list with survivors is in the CSV/JSON.", ""]
        by_src = defaultdict(list)
        for o in bins:
            by_src[_dir(o.path)].append(o)
        for src, group in sorted(by_src.items(), key=lambda kv: -sum(o.size for o in kv[1])):
            survivors = defaultdict(int)
            for o in group:
                survivors[_dir(o.survivor)] += 1
            top = ", ".join(f"`{d}` ({c})" for d, c in
                            sorted(survivors.items(), key=lambda kv: -kv[1])[:3])
            lines.append(f"- `{src}`: {len(group):,} files, "
                         f"{_human(sum(o.size for o in group))} — survivors in {top}")
        lines.append("")

    rms = [o for o in ops if o.kind == "REMOVE_FOLDER"]
    if rms:
        lines += ["## REMOVE_FOLDER — empty after the above", ""]
        lines += [f"- `{o.path}`" for o in rms]
        lines.append("")

    cons = [o for o in ops if o.kind == "CONFLICT"]
    if cons:
        lines += ["## CONFLICT — not executed; decide by hand", ""]
        lines += [f"- `{o.path}` → `{o.destination}`: {o.reason}" for o in cons]
        lines.append("")
    base.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
