# mega-clean

Review duplicates in a MEGA photo library and upload only the local photos that
are not already there. **This tool never permanently deletes anything.** The
one destructive-looking action it has, `bin`, moves files to MEGA's Rubbish
Bin from a plan you have reviewed — reversible until *you* empty the bin, which
the tool cannot do. A test enforces that no permanent-delete code exists.

## Why it works the way it does

MEGA exposes no content hash. Neither "are these two remote files identical" nor
"is this local photo already up there" can be answered by asking MEGA, so
content identity is derived client-side from bytes fetched over range requests,
in tiers that get more expensive as they get more capable.

## Setup

```bash
brew install rclone
rclone config                 # add a remote named "mega"
pip install -e '.[dev]'
megaclean doctor --remote mega
```

If the account uses two-factor auth, `rclone config` will prompt for a one-time
code (the backend's `2fa` option); it uses that once to establish a session, so
you are not asked again on later runs. MEGAcmd is optional and not needed for
any of this.

Run `megaclean doctor` before anything else. It verifies the assumption the
whole design rests on: that `rclone cat --offset` fetches only the range asked
for rather than streaming the whole file.

## Finding duplicates

```bash
megaclean scan-fingerprints --remote mega --root Photos   # ~25s for 85k files
megaclean dupes --out report.html --csv report.csv --gallery report/
```

`scan-fingerprints` reads MEGA's own content fingerprint out of the account's
metadata: one API call returns the whole node tree, and every file node carries
a CRC its uploading client computed. No file bytes are fetched, so an 85,000
file account is grouped in under half a minute.

That fingerprint is a *sparse* CRC — it samples blocks rather than hashing
everything. Measured against a real account it agreed with the bytes 79 times
in 80. Groups found this way are reported as **fingerprint only**; confirm them
before acting:

```bash
megaclean verify --remote mega --workers 12       # reads bytes, ~3.5 files/s
```

Verified groups are re-reported as **byte-verified**. Verification is
resumable: interrupt it and re-run.

### The gallery

`--gallery DIR` writes a browsable page: groups ranked by reclaimable space,
filterable by path and kind, with pictures. Fetch the pictures first:

```bash
megaclean previews --remote mega --limit 500      # ~2 GB for the top 500 groups
```

One image is fetched per identical group, since its members are the same file;
variant groups fetch every member because those genuinely differ. `--limit`
takes the groups with the most reclaimable space, so a bounded transfer buys
the most useful pictures.

### What "duplicate" means here, precisely

Redundancy is only ever counted **inside a byte-identical subgroup**. A variant
cluster says "these are the same shot", which for a burst sequence means
several *different* photographs — so each distinct picture keeps a copy. One
real group held 9 files that were 3 distinct frames with 3 identical copies
each; only 6 of the 9 are removable, not 8.

### The older, byte-level path

`scan-remote` plus `fingerprint` still exist and need no MEGA-specific
metadata, at the cost of fetching 64 KB per candidate file:

```bash
megaclean scan-remote --remote mega --root Photos
megaclean fingerprint --remote mega --scope collisions
```

`--scope collisions` only inspects files whose byte size collides with another
file, because nothing else can be byte-identical. That is most of the account
skipped.

To find variants — the same photo re-encoded, resized or renamed — run the full
sweep once. It reads the first 64 KB of every image for EXIF and the embedded
camera thumbnail:

```bash
megaclean fingerprint --remote mega --scope all
megaclean dupes --out report.html
```

Both are resumable: interrupt them and re-run, and they pick up what is still
pending. `--retry` re-attempts files that errored.

### Why byte-reading runs a server

rclone's mega backend reloads the account's entire node tree every time the
process starts — about 16 seconds on an 80k-file account — so one `rclone cat`
per file spends all its time re-reading the tree. `fingerprint` therefore starts
a single `rclone serve http` process and issues HTTP range requests against it,
which turns a ~17s per-file cost into well under a second. Pass
`--transport cat` to fall back to one process per file if the server will not
start; expect it to be roughly 25× slower.

**Variant detection is a heuristic.** EXIF matching is the strong signal.
Perceptual hashing depends on the camera's embedded thumbnail surviving the
edit, and some tools strip it; it is also unreliable on near-uniform images
(a photo of a blank wall or a plain gradient carries too little structure for a
stable hash). Read the report; do not act on it blindly.

## Uploading what is missing

```bash
megaclean scan-local ~/Pictures/2024
megaclean plan-upload ~/Pictures/2024 --dest-root Photos --out plan.json
# read plan.json
megaclean upload --plan plan.json
```

Planning and uploading are separate so that the upload step decides nothing.
Every entry carries one of three actions:

- `upload` — not in the account; will be uploaded to the mirrored path.
- `skip` — byte-identical content already exists somewhere in the account, and
  the entry records every path where it lives.
- `review` — an EXIF or perceptual match, but not a byte match. **Not uploaded
  and not skipped.** You decide. `--include-review` uploads them anyway.

`--dry-run` reports what would happen and transfers nothing.

Note that `plan-upload` can only recognise a re-encoded copy already in MEGA if
`fingerprint --scope all` has been run at least once. With only the cheap scope,
a local photo is compared against remote files that share its exact byte size.

## Reclaiming space

```bash
megaclean plan-bin --remote mega --prefer "Library" --only-under "Camera uploads"
# read bin-plan.json: every file lists the copy that survives it
megaclean bin --plan bin-plan.json --dry-run
megaclean bin --plan bin-plan.json --limit 25      # trial batch
megaclean bin --plan bin-plan.json
```

Two invariants hold at planning time and again at execution: nothing in a plan
is ever a keeper (so nothing binned is the last copy), and only byte-verified
copies qualify unless you pass `--allow-unverified`. Then the habit that makes
the residual risk irrelevant: **leave the Rubbish Bin alone for a month**
before emptying it.

## Executing the full manifest

```bash
megaclean plan-ops --prefer "Library" --inbox "Camera uploads"   # ops.md/.csv/.json
megaclean apply --manifest ops.json --dry-run
megaclean apply --manifest ops.json --phase MOVE --limit 10       # trial
megaclean apply --manifest ops.json                               # MOVE, BIN, REMOVE_FOLDER
```

`apply` trusts nothing in the manifest. Every node is located in a freshly
fetched tree; a MOVE is skipped if the file is not where the manifest said, if
the destination is not a live folder, or if a same-named file now exists there;
a BIN is skipped if its survivor is no longer live; a folder is removed only if
the live tree shows it completely empty. CONFLICT and UNVERIFIED entries are
never executed. All writes are moves.

## Tests

```bash
pytest                                  # offline; no account needed
MEGACLEAN_TEST_REMOTE=mega MEGACLEAN_TEST_ROOT=Photos pytest -m integration
```
