"""Reporting. The report suggests a keeper; it never acts on one."""
from __future__ import annotations

import csv
import datetime as _dt
from collections.abc import Sequence
from html import escape
from pathlib import Path

from .dupes import Cluster

_CSV_COLUMNS = ["cluster_id", "kind", "role", "exact_group", "path", "size",
                "width", "height", "mtime"]


def _exact_group_labels(cluster) -> dict[str, str]:
    """Label each member that is byte-identical to another member.

    A variant cluster only claims its members are the same photo, which is a
    heuristic. Members sharing a signature are provably the same bytes, and
    that is the part you can act on with confidence.
    """
    labels: dict[str, str] = {}
    for i, group in enumerate(cluster.exact_groups, start=1):
        for member in group:
            labels[member.path] = str(i)
    return labels


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
            labels = _exact_group_labels(cluster)
            for member in cluster.members:
                writer.writerow({
                    "cluster_id": i,
                    "kind": cluster.kind,
                    "role": ("keeper" if member.path == cluster.keeper.path
                             else "duplicate"),
                    "exact_group": labels.get(member.path, ""),
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
        ".ident{background:#e8eef9;border-radius:3px;padding:0 .35rem;"
        "font-size:.85em;white-space:nowrap}"
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
        labels = _exact_group_labels(cluster)
        parts.append(f"<h2 class='{cluster.kind}'>Cluster {i} — "
                     f"{cluster.kind}</h2>")
        if cluster.kind == "variant" and cluster.exact_groups:
            count = len(cluster.exact_groups)
            parts.append(
                f"<p>This cluster is a heuristic match, but it contains "
                f"{count} byte-identical group{'s' if count > 1 else ''} "
                f"(marked below) whose members are provably the same file.</p>"
            )
        parts.append("<table><tr><th>Role</th><th>Path</th><th>Size</th>"
                     "<th>Dimensions</th><th>Modified</th></tr>")
        for member in cluster.members:
            keeper = member.path == cluster.keeper.path
            dims = (f"{member.width}×{member.height}"
                    if member.width and member.height else "—")
            label = labels.get(member.path)
            badge = (f" <span class='ident'>byte-identical #{label}</span>"
                     if label else "")
            parts.append(
                f"<tr class='{'keeper' if keeper else 'dup'}'>"
                f"<td>{'keep' if keeper else 'duplicate'}</td>"
                f"<td>{escape(member.path)}{badge}</td>"
                f"<td>{_human(member.size)}</td>"
                f"<td>{dims}</td><td>{escape(member.mtime or '')}</td></tr>"
            )
        parts.append("</table>")
    Path(path).write_text("\n".join(parts), encoding="utf-8")
