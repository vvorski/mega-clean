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
            close = []
            for neighbour in trees[n.phash_src].query(n.phash, phash_threshold):
                close.extend(owners[n.phash_src][neighbour])
            if close:
                matches.extend(close)
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
