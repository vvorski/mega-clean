"""Which local files have no byte-identical copy anywhere in the account.

Uses MEGA's own fingerprint computed locally, so every live remote file is a
candidate, not only the ones we have byte-signatures for. "Missing" is a firm
claim -- identical bytes always give the same fingerprint. "Present" is a
strong one: the fingerprint is sparse and matched the bytes 79 times in 80
when measured; confirm before relying on it to skip an upload.
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from .megacrc import mega_crc

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Comparison:
    path: str                  # relative to the local root
    size: int
    crc: str
    status: str                # "present" | "missing" | "error"
    remote_path: str = ""      # one place the same content lives, if any


def _iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        yield p, rel.as_posix()


def compare_local(conn, root: Path, *,
                  on_progress: Callable[[int], None] | None = None
                  ) -> list[Comparison]:
    root = Path(root).resolve()
    remote: dict[str, str] = {}
    for r in conn.execute('SELECT crc, path FROM nodes WHERE scope = "remote" '
                          'AND crc IS NOT NULL ORDER BY path'):
        remote.setdefault(r["crc"], r["path"])
    out: list[Comparison] = []
    for i, (p, rel) in enumerate(_iter_files(root), start=1):
        try:
            size = p.stat().st_size
            crc = mega_crc(p).hex()
        except OSError as exc:
            out.append(Comparison(rel, 0, "", "error", str(exc)))
            continue
        hit = remote.get(crc)
        out.append(Comparison(rel, size, crc,
                              "present" if hit else "missing", hit or ""))
        if on_progress and i % 1000 == 0:
            on_progress(i)
    return out


def _human(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"


def write_comparison(result: Sequence[Comparison], out_base: Path, *,
                     local_root: Path) -> None:
    base = Path(out_base)
    base.parent.mkdir(parents=True, exist_ok=True)
    missing = [r for r in result if r.status == "missing"]
    present = [r for r in result if r.status == "present"]
    errors = [r for r in result if r.status == "error"]

    (base.parent / (base.name + ".json")).write_text(json.dumps({
        "generated": _dt.datetime.now().isoformat(timespec="seconds"),
        "local_root": str(local_root),
        "counts": {"present": len(present), "missing": len(missing),
                   "error": len(errors)},
        "missing": [asdict(r) for r in missing],
        "present": [asdict(r) for r in present],
    }, indent=1), encoding="utf-8")

    with (base.parent / (base.name + ".csv")).open("w", newline="",
                                                    encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "size", "crc"])
        for r in missing:
            w.writerow([r.path, r.size, r.crc])

    by_top: dict[str, list[Comparison]] = defaultdict(list)
    for r in missing:
        by_top[r.path.split("/", 1)[0] if "/" in r.path else "/"].append(r)
    ext = Counter(Path(r.path).suffix.lower() or "(none)" for r in missing)
    lines = [
        f"# Files in `{local_root}` with no copy in MEGA",
        "",
        f"Scanned {len(result):,} files. **{len(present):,} already in MEGA** "
        f"(byte-identical content somewhere in the account), "
        f"**{len(missing):,} not in MEGA** ({_human(sum(r.size for r in missing))}), "
        f"{len(errors)} unreadable.",
        "",
        "Compared by MEGA's own content fingerprint, so a file counts as present "
        "wherever it lives and whatever it is called. \"Not in MEGA\" is a firm "
        "claim; \"present\" is a strong one (the fingerprint is sparse, right "
        "~79 times in 80).",
        "",
        "## Missing, by top-level folder", "",
    ]
    for top, rs in sorted(by_top.items(), key=lambda kv: -sum(r.size for r in kv[1])):
        lines.append(f"- `{top}`: {len(rs):,} files, {_human(sum(r.size for r in rs))}")
    lines += ["", "## Missing, by type", ""]
    lines += [f"- `{e}`: {n:,}" for e, n in ext.most_common(12)]
    lines += ["", "## Every missing file", "",
              "Full list also in the CSV (path, size, crc).", ""]
    lines += [f"- `{r.path}` ({_human(r.size)})" for r in missing]
    if errors:
        lines += ["", "## Unreadable", ""]
        lines += [f"- `{r.path}`: {r.remote_path}" for r in errors]
    (base.parent / (base.name + ".md")).write_text("\n".join(lines) + "\n",
                                                   encoding="utf-8")
