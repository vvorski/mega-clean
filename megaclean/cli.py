"""Command-line entry point.

`remote_factory` is injectable so every flow below is exercised in tests
against an in-memory account.
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path

from . import __version__
from .dupes import cluster_nodes, nodes_from_rows
from .fingerprint import run_fingerprint
from .index import (
    clear_errors, iter_nodes, open_index, pending_paths, size_collision_paths,
    upsert_node,
)
from .local import local_scope, scan_local
from .planner import build_plan, read_plan, write_plan
from .remote import RcloneRemote, Remote, join_remote_path
from .report import summarize, write_csv, write_html
from .uploader import execute_plan

DEFAULT_DB = ".megaclean/index.db"


def _cmd_scan_remote(args, conn, make_remote) -> int:
    remote = make_remote(args.remote)
    count = 0
    for f in remote.list_files(args.root):
        # Store the full account path: read_range and upload address the
        # account, so a root-relative path would resolve to nothing.
        upsert_node(conn, "remote", join_remote_path(args.root, f.path),
                    f.size, f.mtime)
        count += 1
    print(f"indexed {count} remote files under {args.remote}:{args.root}")
    return 0


def _cmd_fingerprint(args, conn, make_remote) -> int:
    if args.retry:
        print(f"cleared {clear_errors(conn, 'remote')} previous errors")
    sizes = {r["path"]: r["size"] for r in iter_nodes(conn, "remote")}
    if not sizes:
        print("nothing indexed yet — run scan-remote first", file=sys.stderr)
        return 1
    if args.scope == "collisions":
        candidates = size_collision_paths(conn, "remote")
    else:
        candidates = list(sizes)
    paths = pending_paths(conn, "remote", candidates)
    if not paths:
        print("nothing pending")
        return 0
    print(f"fingerprinting {len(paths)} files (scope={args.scope}, "
          f"workers={args.workers})")
    ok, failed = run_fingerprint(conn, make_remote(args.remote), "remote",
                                 paths, workers=args.workers, sizes=sizes)
    print(f"fingerprinted {ok}, failed {failed}")
    return 0 if failed == 0 else 1


def _cmd_dupes(args, conn, make_remote) -> int:
    nodes = nodes_from_rows(iter_nodes(conn, "remote"))
    if not nodes:
        print("no fingerprinted files — run fingerprint first", file=sys.stderr)
        return 1
    clusters = cluster_nodes(nodes, phash_threshold=args.threshold)
    write_html(clusters, Path(args.out))
    if args.csv:
        write_csv(clusters, Path(args.csv))
    stats = summarize(clusters)
    print(f"{stats['clusters']} clusters "
          f"({stats['exact_clusters']} exact, "
          f"{stats['variant_clusters']} variant), "
          f"{stats['redundant_files']} redundant files, "
          f"{stats['redundant_bytes'] / 1e9:.2f} GB reclaimable")
    print(f"report written to {args.out}")
    return 0


def _cmd_scan_local(args, conn, make_remote) -> int:
    root = Path(args.path).resolve()
    scanned, failed = scan_local(conn, root, include_video=args.include_video)
    print(f"scanned {scanned} local files, {failed} unreadable")
    return 0


def _cmd_plan_upload(args, conn, make_remote) -> int:
    root = Path(args.path).resolve()
    local_nodes = nodes_from_rows(iter_nodes(conn, local_scope(root)))
    if not local_nodes:
        print(f"no local files indexed for {root} — run scan-local first",
              file=sys.stderr)
        return 1
    remote_nodes = nodes_from_rows(iter_nodes(conn, "remote"))
    entries = build_plan(local_nodes, remote_nodes, local_root=str(root),
                         dest_root=args.dest_root,
                         phash_threshold=args.threshold)
    write_plan(entries, Path(args.out), local_root=str(root),
               dest_root=args.dest_root, remote=args.remote)
    counts = {a: sum(1 for e in entries if e.action == a)
              for a in ("upload", "skip", "review")}
    to_upload = sum(e.size for e in entries if e.action == "upload")
    print(f"{counts['upload']} to upload ({to_upload / 1e9:.2f} GB), "
          f"{counts['skip']} already present, {counts['review']} to review")
    print(f"plan written to {args.out} — review it, then run: "
          f"megaclean upload --plan {args.out}")
    return 0


def _cmd_upload(args, conn, make_remote) -> int:
    entries, header = read_plan(Path(args.plan))
    remote_name = args.remote or header.get("remote")
    result = execute_plan(
        conn, make_remote(remote_name), entries,
        local_root=Path(header["local_root"]), scope="remote",
        include_review=args.include_review, dry_run=args.dry_run,
    )
    verb = "would upload" if args.dry_run else "uploaded"
    print(f"{verb} {result.uploaded}, skipped {result.skipped}, "
          f"failed {result.failed}")
    for path, error in result.errors[:20]:
        print(f"  {path}: {error}", file=sys.stderr)
    return 0 if result.failed == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="megaclean",
        description="Review duplicates in a MEGA photo library and upload "
                    "only what is missing. This tool never deletes anything.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--db", default=DEFAULT_DB, help="index database path")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan-remote", help="index the account (free)")
    p.add_argument("--remote", default="mega", help="rclone remote name")
    p.add_argument("--root", default="", help="path within the remote")
    p.set_defaults(func=_cmd_scan_remote)

    p = sub.add_parser("fingerprint", help="fetch bytes needed for identity")
    p.add_argument("--remote", default="mega")
    p.add_argument("--root", default="")
    p.add_argument("--scope", choices=("collisions", "all"),
                   default="collisions",
                   help="collisions: only size-colliding files (cheap, exact "
                        "duplicates). all: every file (enables variant "
                        "detection)")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--retry", action="store_true",
                   help="retry files that previously errored")
    p.set_defaults(func=_cmd_fingerprint)

    p = sub.add_parser("dupes", help="write the duplicate report")
    p.add_argument("--out", default="report.html")
    p.add_argument("--csv", default=None)
    p.add_argument("--threshold", type=int, default=6,
                   help="perceptual hash Hamming distance")
    p.set_defaults(func=_cmd_dupes)

    p = sub.add_parser("scan-local", help="index a local folder")
    p.add_argument("path")
    p.add_argument("--include-video", action="store_true")
    p.set_defaults(func=_cmd_scan_local)

    p = sub.add_parser("plan-upload", help="decide what is missing")
    p.add_argument("path")
    p.add_argument("--dest-root", default="Photos")
    p.add_argument("--out", default="plan.json")
    p.add_argument("--remote", default="mega")
    p.add_argument("--threshold", type=int, default=6)
    p.set_defaults(func=_cmd_plan_upload)

    p = sub.add_parser("upload", help="execute an approved plan")
    p.add_argument("--plan", required=True)
    p.add_argument("--remote", default=None)
    p.add_argument("--include-review", action="store_true",
                   help="also upload entries flagged as possible variants")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_cmd_upload)
    return parser


def main(argv: list[str] | None = None, *,
         remote_factory: Callable[[str], Remote] = RcloneRemote) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    conn = open_index(args.db)
    try:
        return args.func(args, conn, remote_factory)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
