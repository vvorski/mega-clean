# mega-clean: MEGA photo dedupe review and incremental upload

**Date:** 2026-09-10
**Status:** Approved design

## Problem

A MEGA account holds roughly 10k-100k photos across 50-500 GB. Two needs:

1. Review what is there and find duplicates -- both byte-identical copies and
   variants of the same photo (re-encoded, resized, renamed).
2. Given a local folder, determine which photos are not yet in the account and
   upload only those.

## The governing constraint

MEGA exposes no content hash. `rclone` reports no MD5/SHA for its mega backend,
and MEGAcmd does not print node fingerprints. Neither "are these two remote
files identical" nor "is this local photo already up there" can be answered by
asking MEGA. Content identity must be derived client-side, and every design
decision below follows from the cost of doing that over a 50-500 GB account.

## Approach: tiered signatures over range reads

Three tiers, each strictly more expensive, each cached permanently in SQLite:

- **Tier 0 -- listing (free).** `rclone lsjson -R` yields path, size and mtime
  for every node.
- **Tier 1 -- exact identity (cheap).** Only files whose byte size collides with
  another file can possibly be byte-identical. For those files only, read the
  first and last 64 KB by range request and hash them with the size. Most of the
  account is skipped entirely.
- **Tier 2 -- variant identity (opt-in, expensive).** For every image, read the
  first 64 KB, which carries the EXIF block and usually a camera-embedded JPEG
  thumbnail. Yields the EXIF grouping key and a perceptual hash without
  downloading full images.

Tier 1 answers "already uploaded" for byte-identical files. Tier 2 is what makes
variant detection possible at all; until it has run once, a re-encoded copy
already in MEGA will not be recognised.

## Identity rules

**Exact signature.** `blake2b-128(size_le64 || head[:65536] || tail[-65536:])`.
For files of 128 KB or less the head and tail overlap, so the whole file is
hashed instead. Two files sharing a signature are treated as byte-identical. The
project never deletes anything, so a false positive here costs a wrong row in a
report, not data.

**EXIF key.** Parsed from the head buffer with `exifread`, which tolerates a
truncated JPEG because EXIF lives in the APP1 segment at the front. The key is
`Make|Model|DateTimeOriginal|SubSecTimeOriginal|ExposureTime|FNumber`.
`DateTimeOriginal` has one-second granularity, so exposure settings and sub-second
time are included to keep burst frames apart. A file with no valid
`DateTimeOriginal` gets no EXIF key and is never grouped by this rule.

**Perceptual hash.** `imagehash.phash` (64-bit) over the embedded EXIF thumbnail
when present, otherwise over the decoded image when full bytes are already in
hand (local files). Each hash records its provenance -- `thumb` or `full` --
and hashes are only ever compared within the same provenance class, since JPEG
thumbnail artifacts shift the hash enough to make cross-class comparison
unreliable. Two hashes within Hamming distance 6 are treated as the same photo.

**Known limitation, stated plainly:** thumbnail-based perceptual hashing only
works when the variant preserved the camera's embedded thumbnail. Tools that
strip or regenerate it (ImageMagick and others commonly do) will produce a
variant this method cannot match by phash. Such files remain matchable by EXIF
key, which is the stronger of the two signals in practice. Variant detection is
a best-effort heuristic and the report presents it as one.

## Clustering and keeper selection

Union-find over remote nodes. Union on: identical signature (marks the cluster
EXACT), identical EXIF key, or perceptual hashes of the same provenance within
Hamming distance 6. A cluster whose members all share one signature is reported
as EXACT; any other cluster is VARIANT. Perceptual-hash neighbours are found via
a BK-tree; brute-force pairwise comparison is quadratic and untenable at 100k
files.

Suggested keeper, deterministic and in order: largest pixel area, then largest
byte size, then earliest mtime, then shallowest path, then lexicographic path.
The report suggests; it does not act.

## Commands

| Command | What it does |
|---|---|
| `scan-remote` | Tier 0 listing into SQLite |
| `fingerprint --scope collisions\|all` | Tier 1 (default) or Tier 2 signature fetch |
| `dupes --out report.html` | Clusters and writes HTML + CSV report |
| `scan-local PATH` | Same signatures computed directly over a local folder |
| `plan-upload PATH --out plan.json` | Diffs local against the remote index |
| `upload --plan plan.json` | Executes an approved plan |
| `doctor` | Verifies rclone, remote config, range-read efficiency, deps |

`plan-upload` and `upload` are separate so that the upload step decides nothing;
it executes a file the user has read. Plan entries carry one of three actions:

- `upload` -- no remote match; will be uploaded to the mirrored destination path.
- `skip` -- exact signature match found; the plan records every remote path
  where the content already lives.
- `review` -- an EXIF or perceptual match but not a byte match. **Not skipped by
  default.** These are listed for the user to decide; a possible variant is not
  strong enough evidence to silently drop a local file.

## Transport

`rclone` is the transport for all operations. Listing uses `rclone lsjson
--recursive --files-only`; range reads use `rclone cat --offset/--count`, with a
negative offset for the tail read; uploads use `rclone copyto`. MEGAcmd is
optional -- useful for logging in when 2FA is enabled, which rclone's mega
backend does not handle, and available as an alternative uploader. MEGAcmd
cannot serve partial reads, so fingerprinting requires rclone regardless.

**Assumption requiring verification before build:** that `rclone cat --offset`
against the mega backend transfers only the requested range rather than
streaming the whole file. `doctor` measures this directly. If the assumption
fails, the fallback is full content fetch restricted to size-collision groups --
Tier 1 survives, Tier 2 becomes impractical and gets gated behind an explicit
flag with a bandwidth estimate.

## Error handling and resumability

The SQLite index is the work queue: the pending set is rows lacking a signature,
so any command can be interrupted and re-run. WAL mode; per-node error rows
recording the failure, with `--retry` re-attempting only those. Fingerprinting
runs a bounded thread pool (default 8) of rclone subprocesses with exponential
backoff on failure, since MEGA throttles aggressive clients.

Non-image files are excluded by extension. Video is excluded by default behind
`--include-video`. HEIC and PNG carry EXIF but rarely an embedded thumbnail, so
they participate in Tier 1 fully and in Tier 2 by EXIF key only.

## Safety

No deletion code exists anywhere in the project. `dupes` produces a report and
nothing else. `upload` only creates. This is a property of the codebase, not a
default that a flag can flip.

## Testing

Signature computation, EXIF key derivation, clustering and keeper selection are
pure functions, unit-tested against fixture images generated at test time with
Pillow -- an original plus known re-encodes, resizes and renames, so variant
detection is tested against variants whose correct grouping is known by
construction.

The transport sits behind a `Remote` protocol with an in-memory fake, so every
CLI flow is tested end to end with no network and no account. One opt-in
integration test exercises a real MEGA folder; it is marked and skipped by
default.

## Stack

Python 3.11+, stdlib `sqlite3` and `argparse`. Third-party: `exifread`,
`Pillow`, `pillow-heif`, `ImageHash`. External: `rclone` (required), MEGAcmd
(optional).

## Amendment — 2026-09-10, Rubbish Bin

The "no deletion code" guarantee is narrowed to **no permanent deletion**. A
`bin` command moves redundant copies to MEGA's Rubbish Bin by node handle,
driven by a reviewed plan file, with two invariants checked at plan time and
again at execution: no planned node is a keeper anywhere, and only
byte-verified copies qualify by default. The tool cannot empty the bin or hard
delete; the guard test now forbids the MEGA `d` command and `hard_delete`.
