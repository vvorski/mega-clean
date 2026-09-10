"""Execute an operations manifest against MEGA, checking everything live.

The manifest was built from an index that may be stale by the time it runs.
So nothing here trusts it: every node is located in a freshly fetched tree,
every destination is confirmed to be a live folder outside the bin, name
collisions are re-checked against live children, and a folder is removed only
if the live tree shows it completely empty. All writes are moves; the bin
keeps everything until the user empties it.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from .megaapi import RUBBISH_TYPE
from .ops import Operation

log = logging.getLogger(__name__)

CLOUD_TYPE = 2
EXECUTABLE = ("MOVE", "BIN", "REMOVE_FOLDER")


@dataclass
class ApplyResult:
    moved: int = 0
    binned: int = 0
    removed: int = 0
    failed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


class _Tree:
    """A snapshot of the account tree we can walk and update locally."""

    def __init__(self, nodes: Sequence[dict], names: dict[str, str]) -> None:
        self.by_h = {n["h"]: dict(n) for n in nodes}
        self.names = names

    def root_type(self, h: str) -> int | None:
        seen = 0
        while h in self.by_h and seen < 64:
            parent = self.by_h[h].get("p")
            if parent not in self.by_h:
                return self.by_h[h].get("t")
            h = parent
            seen += 1
        return None

    def path(self, h: str) -> str | None:
        parts, seen = [], 0
        while h in self.by_h and seen < 64:
            name = self.names.get(h)
            if name is None:
                break
            parts.append(name)
            h = self.by_h[h].get("p")
            seen += 1
        return "/".join(reversed(parts)) if parts else None

    def children(self, h: str) -> list[dict]:
        return [n for n in self.by_h.values() if n.get("p") == h]

    def is_live_folder(self, h: str) -> bool:
        n = self.by_h.get(h)
        return bool(n) and n.get("t") == 1 and self.root_type(h) == CLOUD_TYPE

    def move(self, h: str, target: str) -> None:
        self.by_h[h]["p"] = target


def _check_invariants(ops: Sequence[Operation]) -> None:
    removed = {o.path for o in ops if o.kind == "REMOVE_FOLDER"}
    bad = [o for o in ops if o.kind == "MOVE" and o.destination in removed]
    if bad:
        raise ValueError(f"MOVE destination is itself being removed: "
                         f"{bad[0].destination}")
    binned = {o.handle for o in ops if o.kind == "BIN"}
    survivors = {o.survivor_handle for o in ops if o.kind == "BIN"}
    if binned & survivors:
        raise ValueError("manifest bins a survivor")


def apply_operations(api, ops: Sequence[Operation], *, rubbish: str,
                     dry_run: bool = False,
                     phases: Sequence[str] = EXECUTABLE,
                     limit: int | None = None,
                     on_progress=None) -> ApplyResult:
    _check_invariants(ops)
    result = ApplyResult()
    budget = [limit if limit is not None else float("inf")]

    def fail(op: Operation, why: str) -> None:
        result.failed += 1
        result.errors.append((op.path, why))
        log.warning("%s %s: %s", op.kind, op.path, why)
        if on_progress:
            on_progress(op, False)

    def fresh() -> _Tree:
        nodes = api.fetch_nodes()
        return _Tree(nodes, api.node_names(nodes))

    tree = fresh()
    wanted = set(phases) & set(EXECUTABLE)

    # ---- MOVE -------------------------------------------------------------
    if "MOVE" in wanted:
        by_dest: dict[str, list[Operation]] = defaultdict(list)
        for op in ops:
            if op.kind == "MOVE":
                by_dest[op.destination_handle].append(op)
        for dest, group in by_dest.items():
            if not tree.is_live_folder(dest):
                for op in group:
                    fail(op, f"destination `{op.destination}` is not a live folder")
                continue
            live_names = {tree.names.get(c["h"]) for c in tree.children(dest)}
            todo = []
            for op in group:
                if budget[0] <= 0:
                    break
                if tree.root_type(op.handle) != CLOUD_TYPE or tree.path(op.handle) != op.path:
                    fail(op, "not where the manifest expected (index is stale)")
                    continue
                name = op.path.rsplit("/", 1)[-1]
                if name in live_names:
                    fail(op, f"`{op.destination}` already holds a file named {name}")
                    continue
                live_names.add(name)
                todo.append(op)
                budget[0] -= 1
            if not todo:
                continue
            codes = ({op.handle: 0 for op in todo} if dry_run
                     else api.move_nodes([op.handle for op in todo], dest))
            for op in todo:
                if codes.get(op.handle) == 0:
                    tree.move(op.handle, dest)
                    result.moved += 1
                    if on_progress:
                        on_progress(op, True)
                else:
                    fail(op, f"MEGA error {codes.get(op.handle)}")

    # ---- BIN --------------------------------------------------------------
    if "BIN" in wanted:
        todo = []
        for op in ops:
            if op.kind != "BIN" or budget[0] <= 0:
                continue
            if tree.root_type(op.handle) != CLOUD_TYPE or tree.path(op.handle) != op.path:
                fail(op, "not where the manifest expected (index is stale)")
                continue
            if tree.root_type(op.survivor_handle) != CLOUD_TYPE:
                fail(op, "survivor is no longer live; refusing to bin")
                continue
            todo.append(op)
            budget[0] -= 1
        if todo:
            codes = ({op.handle: 0 for op in todo} if dry_run
                     else api.move_to_rubbish([op.handle for op in todo], rubbish))
            for op in todo:
                if codes.get(op.handle) == 0:
                    tree.move(op.handle, rubbish)
                    result.binned += 1
                    if on_progress:
                        on_progress(op, True)
                else:
                    fail(op, f"MEGA error {codes.get(op.handle)}")

    # ---- REMOVE_FOLDER ----------------------------------------------------
    if "REMOVE_FOLDER" in wanted:
        tree = fresh() if not dry_run else tree      # the live truth, not ours
        for op in ops:
            if op.kind != "REMOVE_FOLDER" or budget[0] <= 0:
                continue
            if not tree.is_live_folder(op.handle) or tree.path(op.handle) != op.path:
                fail(op, "folder is not where the manifest expected")
                continue
            kids = tree.children(op.handle)
            if kids:
                fail(op, f"not empty in the live tree ({len(kids)} items); left in place")
                continue
            budget[0] -= 1
            code = 0 if dry_run else api.move_to_rubbish([op.handle], rubbish).get(op.handle)
            if code == 0:
                tree.move(op.handle, rubbish)
                result.removed += 1
                if on_progress:
                    on_progress(op, True)
            else:
                fail(op, f"MEGA error {code}")
    return result
