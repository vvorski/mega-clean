"""Folder-level view of duplication.

Thousands of individual file decisions are unworkable; a few dozen folder
decisions are not. This asks, for each directory: how much of it exists
elsewhere, where that elsewhere is, and -- the part that matters -- what would
be lost if the folder were removed.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .dupes import Cluster, Node


def _dir(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else "/"


@dataclass(frozen=True)
class FolderOverlap:
    folder: str
    files: int
    total_bytes: int
    duplicated_files: int
    duplicated_bytes: int
    unique_files: int
    unique_bytes: int
    counterparts: tuple[tuple[str, int, int], ...]   # (folder, files, bytes)
    unique_examples: tuple[str, ...]

    @property
    def coverage(self) -> float:
        """Fraction of this folder that also exists in some other folder."""
        return self.duplicated_files / self.files if self.files else 0.0


def analyse_folders(clusters: Sequence[Cluster], nodes: Sequence[Node], *,
                    min_files: int = 10) -> list[FolderOverlap]:
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for node in nodes:
        entry = totals[_dir(node.path)]
        entry[0] += 1
        entry[1] += node.size

    elsewhere: dict[str, dict[str, None]] = defaultdict(dict)
    shared_files: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    shared_bytes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for cluster in clusters:
        for group in cluster.content_groups:
            if len(group) < 2:
                continue
            by_dir: dict[str, list[Node]] = defaultdict(list)
            for member in group:
                by_dir[_dir(member.path)].append(member)
            if len(by_dir) < 2:
                continue        # copies confined to one folder prove nothing
            for folder, members in by_dir.items():
                for member in members:
                    elsewhere[folder][member.key] = None
                for other, other_members in by_dir.items():
                    if other == folder:
                        continue
                    shared_files[folder][other] += len(other_members)
                    shared_bytes[folder][other] += sum(m.size for m in other_members)

    by_folder: dict[str, list[Node]] = defaultdict(list)
    for node in nodes:
        by_folder[_dir(node.path)].append(node)

    results = []
    for folder, (count, size) in totals.items():
        if count < min_files:
            continue
        dup_keys = elsewhere.get(folder, {})
        members = by_folder[folder]
        dup = [m for m in members if m.key in dup_keys]
        uniq = [m for m in members if m.key not in dup_keys]
        counterparts = tuple(sorted(
            ((other, shared_files[folder][other], shared_bytes[folder][other])
             for other in shared_files.get(folder, ())),
            key=lambda row: -row[2]))
        results.append(FolderOverlap(
            folder=folder, files=count, total_bytes=size,
            duplicated_files=len(dup),
            duplicated_bytes=sum(m.size for m in dup),
            unique_files=len(uniq), unique_bytes=sum(m.size for m in uniq),
            counterparts=counterparts,
            unique_examples=tuple(m.path.rsplit("/", 1)[-1]
                                  for m in sorted(uniq, key=lambda m: -m.size)[:6]),
        ))
    results.sort(key=lambda r: -r.duplicated_bytes)
    return results


@dataclass(frozen=True)
class MergeDecision:
    source: str                 # the folder to collapse
    destination: str            # the folder that survives
    shared_files: int
    shared_bytes: int
    must_move_files: int        # unique to source, would be lost
    must_move_bytes: int
    must_move_examples: tuple[str, ...]
    other_counterparts: tuple[str, ...]
    source_is_inbox: bool = False
    backlog_files: int = 0
    backlog_bytes: int = 0


def _preference(folder: str, prefer: Sequence[str]) -> int:
    for i, prefix in enumerate(prefer):
        if folder.startswith(prefix):
            return i
    return len(prefer)


def merge_decisions(overlaps: Sequence[FolderOverlap],
                    prefer: Sequence[str] = (),
                    min_coverage: float = 0.5,
                    inbox: Sequence[str] = ()) -> list[MergeDecision]:
    """One directional decision per overlapping pair.

    Direction is chosen by: the canonical folder survives; failing that the
    folder with more unique content survives, because that means moving fewer
    files. Reciprocal pairs collapse to a single decision rather than two
    contradictory ones.

    Folders named in `inbox` are auto-sync targets. They are drained, never
    removed -- a phone would simply refill them -- so their unique files are
    reported as a filing backlog rather than as a blocker to deletion.
    """
    index = {o.folder: o for o in overlaps}
    decisions: list[MergeDecision] = []
    seen: set[frozenset[str]] = set()

    for overlap in overlaps:
        if overlap.coverage < min_coverage or not overlap.counterparts:
            continue
        other_name, files, byts = overlap.counterparts[0]
        pair = frozenset((overlap.folder, other_name))
        if pair in seen:
            continue
        seen.add(pair)
        other = index.get(other_name)

        mine = (_preference(overlap.folder, prefer),
                -overlap.unique_files, -overlap.total_bytes, overlap.folder)
        theirs = (_preference(other_name, prefer),
                  -(other.unique_files if other else 0),
                  -(other.total_bytes if other else 0), other_name)
        # Lower sorts first and survives.
        if theirs <= mine:
            source, destination, keep = overlap, other_name, other
        else:
            source, destination, keep = (other, overlap.folder, overlap)
            if source is None:
                continue

        is_inbox = _preference(source.folder, inbox) < len(inbox)
        decisions.append(MergeDecision(
            source=source.folder, destination=destination,
            shared_files=files, shared_bytes=byts,
            must_move_files=0 if is_inbox else source.unique_files,
            must_move_bytes=0 if is_inbox else source.unique_bytes,
            must_move_examples=() if is_inbox else source.unique_examples,
            source_is_inbox=is_inbox,
            backlog_files=source.unique_files if is_inbox else 0,
            backlog_bytes=source.unique_bytes if is_inbox else 0,
            other_counterparts=tuple(
                name for name, _, _ in source.counterparts[1:4]),
        ))
    decisions.sort(key=lambda d: -d.shared_bytes)
    return decisions


def chained_decisions(decisions: Sequence[MergeDecision]
                      ) -> list[tuple[str, int, int]]:
    """Folders that are removed by one decision and written to by another.

    Executing the list top-down would then move files into a folder that has
    already gone. Each entry is (folder, decision_removing_it,
    decision_writing_into_it), numbered as the plan numbers them.
    """
    removed = {d.source: i for i, d in enumerate(decisions, start=1)
               if not d.source_is_inbox}
    chains = []
    for i, d in enumerate(decisions, start=1):
        if d.destination in removed:
            chains.append((d.destination, removed[d.destination], i))
    return sorted(chains, key=lambda row: row[1])


def _human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def write_folder_plan(overlaps: Sequence[FolderOverlap], path: Path, *,
                      min_coverage: float = 0.5,
                      prefer: Sequence[str] = (),
                      inbox: Sequence[str] = ()) -> Path:
    """A markdown worklist: one directional decision per overlapping pair."""
    lines = [
        "# Folder cleanup plan",
        "",
        "Each entry is one decision. **Nothing here has been deleted** — this "
        "tool cannot delete. Work top-down; the list is ordered by how much "
        "space the folder's duplicated content occupies.",
        "",
        "Before removing any folder, move its **unique** files somewhere safe: "
        "those are the ones that exist nowhere else.",
        "",
    ]
    decisions = merge_decisions(overlaps, prefer, min_coverage, inbox)
    total = sum(d.shared_bytes for d in decisions)
    lines += [f"{len(decisions)} decisions, covering {_human(total)} of "
              f"content stored twice.", ""]
    if prefer:
        lines += [f"Canonical folders (these survive): "
                  + ", ".join(f"`{p}`" for p in prefer), ""]

    chains = chained_decisions(decisions)
    if chains:
        lines += [
            "> **Order matters for a few of these.** The folders below are "
            "removed by one decision and used as a destination by another, so "
            "doing them in listed order would move files into a folder that is "
            "already gone. Do the later decision first, or pick a different "
            "destination.",
            "",
        ]
        for folder, removed_by, written_by in chains:
            lines.append(f">  - `{folder}` — removed by #{removed_by}, "
                         f"written into by #{written_by}: do #{written_by} first")
        lines.append("")

    for i, d in enumerate(decisions, start=1):
        warn = ""
        if any(d.destination == f for f, _, _ in chains):
            warn = "  ⚠️ destination is removed by another decision"
        elif any(d.source == f for f, _, _ in chains):
            warn = "  ⚠️ another decision writes into this folder"
        lines += [
            f"## {i}. `{d.source}`  →  `{d.destination}`{warn}",
            "",
            f"- {d.shared_files:,} files ({_human(d.shared_bytes)}) are in both",
            f"- unique to `{d.source}`: {d.must_move_files:,} files "
            f"({_human(d.must_move_bytes)})",
            "",
        ]
        if d.source_is_inbox:
            lines += [
                f"`{d.source}` is an auto-sync inbox — it refills itself, so it "
                f"is drained, never removed.",
                "",
                f"**Delete the {d.shared_files:,} files "
                f"({_human(d.shared_bytes)}) already filed in "
                f"`{d.destination}`.**",
                "",
                f"Separately, {d.backlog_files:,} files "
                f"({_human(d.backlog_bytes)}) here are still to file — that is "
                f"a backlog, not redundancy.",
                "",
            ]
        elif d.must_move_files == 0:
            lines += [f"**Safe to remove `{d.source}`** — nothing unique in it.",
                      ""]
        else:
            lines += [
                f"**Move {d.must_move_files:,} unique file"
                f"{'s' if d.must_move_files != 1 else ''} "
                f"({_human(d.must_move_bytes)}) into `{d.destination}` first**, "
                f"then remove `{d.source}`.",
                "",
                "Largest files that exist only in the source:",
                "",
            ]
            lines += [f"  - `{name}`" for name in d.must_move_examples]
            lines.append("")
        if d.other_counterparts:
            lines += ["Also overlaps: "
                      + ", ".join(f"`{n}`" for n in d.other_counterparts), ""]

    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return Path(path)
