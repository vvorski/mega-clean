"""Environment checks, including the one assumption the design rests on."""
from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .remote import Remote
from .signature import HEAD_BYTES

# If partial reads work, head-read time is roughly independent of file size.
# These bounds are deliberately loose: this is meant to catch whole-file
# streaming, not to benchmark a link.
SLOWDOWN_FACTOR = 4.0
SLOWDOWN_GRACE_SECONDS = 2.0


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def check_rclone(runner: Callable = subprocess.run,
                 binary: str = "rclone") -> Check:
    try:
        result = runner([binary, "version"], capture_output=True)
    except FileNotFoundError:
        return Check("rclone", False,
                     "rclone not found on PATH — install it with "
                     "`brew install rclone`")
    if result.returncode != 0:
        return Check("rclone", False, "`rclone version` exited nonzero")
    first = (result.stdout or b"").decode("utf-8", "replace").splitlines()
    return Check("rclone", True, first[0] if first else "present")


def check_remote_configured(remote: str, runner: Callable = subprocess.run,
                            binary: str = "rclone") -> Check:
    try:
        result = runner([binary, "listremotes"], capture_output=True)
    except FileNotFoundError:
        return Check("remote", False, "rclone not found on PATH")
    names = (result.stdout or b"").decode("utf-8", "replace").split()
    configured = [n.rstrip(":") for n in names]
    if remote.rstrip(":") in configured:
        return Check("remote", True, f"{remote}: configured")
    return Check("remote", False,
                 f"no rclone remote named {remote!r}; configured: "
                 f"{', '.join(configured) or 'none'}. Run `rclone config`, or "
                 f"log in with MEGAcmd first if the account uses 2FA")


def check_range_reads(remote: Remote, samples: Sequence[tuple[str, int]],
                      clock: Callable[[], float] = time.monotonic) -> Check:
    """Verify partial reads transfer only the requested range.

    The whole tiered design assumes this. If it fails, the fallback is fetching
    full content for size-collision groups only, and variant detection becomes
    impractical.
    """
    if len(samples) < 2:
        return Check("range reads", False,
                     "not enough indexed files to test — run scan-remote first")
    ordered = sorted(samples, key=lambda s: s[1])
    (small_path, small_size), (big_path, big_size) = ordered[0], ordered[-1]
    try:
        start = clock()
        remote.read_range(small_path, 0, HEAD_BYTES)
        small_elapsed = clock() - start
        start = clock()
        remote.read_range(big_path, 0, HEAD_BYTES)
        big_elapsed = clock() - start
    except Exception as exc:
        return Check("range reads", False, f"range read failed: {exc}")

    budget = small_elapsed * SLOWDOWN_FACTOR + SLOWDOWN_GRACE_SECONDS
    if big_elapsed > budget:
        return Check("range reads", False,
                     f"reading 64 KB from a {big_size / 1e6:.0f} MB file took "
                     f"{big_elapsed:.1f}s versus {small_elapsed:.1f}s from a "
                     f"{small_size / 1e6:.1f} MB file — rclone appears to be "
                     f"downloading the whole file. Use --scope collisions only "
                     f"and expect full-file transfer costs")
    return Check("range reads", True,
                 f"partial reads look genuine ({small_elapsed:.2f}s vs "
                 f"{big_elapsed:.2f}s for 64 KB)")


def run_checks(remote_name: str, remote: Remote,
               samples: Sequence[tuple[str, int]],
               runner: Callable = subprocess.run) -> list[Check]:
    checks = [check_rclone(runner=runner),
              check_remote_configured(remote_name, runner=runner)]
    if checks[1].ok:
        checks.append(check_range_reads(remote, samples))
    return checks
