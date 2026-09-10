# mega-clean

Review duplicates in a MEGA photo library and upload only the local photos that
are not already there. **This tool never deletes anything** — `dupes` writes a
report, `upload` only creates.

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

If the account uses two-factor auth, rclone's mega backend cannot log in;
install MEGAcmd (`brew install megacmd`), authenticate there, and configure
rclone against the same account.

Run `megaclean doctor` before anything else. It verifies the assumption the
whole design rests on: that `rclone cat --offset` fetches only the range asked
for rather than streaming the whole file.

## Finding duplicates

```bash
megaclean scan-remote --remote mega --root Photos      # free: paths and sizes
megaclean fingerprint --remote mega --scope collisions # cheap: exact duplicates
megaclean dupes --out report.html --csv report.csv
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

## Tests

```bash
pytest                                  # offline; no account needed
MEGACLEAN_TEST_REMOTE=mega MEGACLEAN_TEST_ROOT=Photos pytest -m integration
```
