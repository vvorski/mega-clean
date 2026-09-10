import pathlib
import re

from megaclean.doctor import (
    check_range_reads, check_rclone, check_remote_configured,
)
from megaclean.remote import FakeRemote


class Runner:
    def __init__(self, stdout=b"", returncode=0):
        self.stdout, self.returncode, self.stderr = stdout, returncode, b""

    def __call__(self, argv, **kw):
        class R:
            pass

        r = R()
        r.stdout, r.stderr, r.returncode = self.stdout, self.stderr, self.returncode
        return r


def test_rclone_present_reports_version():
    check = check_rclone(runner=Runner(b"rclone v1.74.4\n- os/version: darwin\n"))
    assert check.ok
    assert "v1.74.4" in check.detail


def test_rclone_missing_is_reported_not_raised():
    def boom(argv, **kw):
        raise FileNotFoundError("rclone")

    check = check_rclone(runner=boom)
    assert not check.ok
    assert "not found" in check.detail.lower()


def test_remote_configured_detects_a_missing_remote():
    runner = Runner(b"gdrive:\n")
    assert not check_remote_configured("mega", runner=runner).ok
    assert check_remote_configured("gdrive", runner=runner).ok


def test_range_reads_pass_when_time_is_flat():
    ticks = iter([0.0, 0.5, 1.0, 1.5])
    remote = FakeRemote({"small.jpg": b"x" * 1000, "big.jpg": b"y" * 10_000_000})
    check = check_range_reads(remote,
                              [("small.jpg", 1000), ("big.jpg", 10_000_000)],
                              clock=lambda: next(ticks))
    assert check.ok


def test_range_reads_fail_when_time_scales_with_file_size():
    ticks = iter([0.0, 0.5, 1.0, 60.0])
    remote = FakeRemote({"small.jpg": b"x" * 1000, "big.jpg": b"y" * 10_000_000})
    check = check_range_reads(remote,
                              [("small.jpg", 1000), ("big.jpg", 10_000_000)],
                              clock=lambda: next(ticks))
    assert not check.ok
    assert "whole file" in check.detail


def test_range_reads_need_two_samples():
    check = check_range_reads(FakeRemote({}), [])
    assert not check.ok
    assert "scan-remote" in check.detail


def test_range_read_error_is_reported():
    check = check_range_reads(FakeRemote({}), [("gone.jpg", 1), ("also.jpg", 2)])
    assert not check.ok


def test_no_deletion_capability_exists_in_the_package():
    """The no-delete property is enforced by the codebase, not by a default."""
    # Matches code, not prose: rclone's destructive subcommands as they would
    # appear in an argv list, and Python's removal calls.
    forbidden = re.compile(
        r"""["'](?:delete|deletefile|purge|rmdir|rmdirs)["']"""
        r"""|\bos\.remove\(|\bos\.unlink\(|\bshutil\.rmtree\("""
        r"""|\.unlink\(|\bmega-rm\b"""
    )
    package = pathlib.Path(__file__).resolve().parent.parent / "megaclean"
    offenders = [f.name for f in package.glob("*.py")
                 if forbidden.search(f.read_text())]
    assert offenders == [], f"deletion capability found in: {offenders}"
