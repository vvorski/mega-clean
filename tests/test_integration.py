"""Opt-in checks against a real MEGA account.

Run with: MEGACLEAN_TEST_REMOTE=mega MEGACLEAN_TEST_ROOT=Photos \
          pytest -m integration
"""
import os

import pytest

from megaclean.doctor import check_range_reads, check_remote_configured
from megaclean.remote import RcloneRemote

pytestmark = pytest.mark.integration

REMOTE = os.environ.get("MEGACLEAN_TEST_REMOTE", "")
ROOT = os.environ.get("MEGACLEAN_TEST_ROOT", "")


@pytest.fixture
def remote():
    if not REMOTE:
        pytest.skip("set MEGACLEAN_TEST_REMOTE to run integration tests")
    return RcloneRemote(REMOTE)


def test_remote_is_configured():
    if not REMOTE:
        pytest.skip("set MEGACLEAN_TEST_REMOTE to run integration tests")
    assert check_remote_configured(REMOTE).ok


def test_listing_returns_files(remote):
    files = list(remote.list_files(ROOT))
    assert files, f"no files found under {REMOTE}:{ROOT}"


def test_partial_reads_are_genuinely_partial(remote):
    samples = [(f.path, f.size) for f in remote.list_files(ROOT)]
    check = check_range_reads(remote, samples)
    assert check.ok, check.detail


def test_head_read_returns_the_requested_length(remote):
    from megaclean.signature import HEAD_BYTES
    big = max(remote.list_files(ROOT), key=lambda f: f.size)
    assert big.size > HEAD_BYTES, "no file large enough to test a bounded read"
    assert len(remote.read_range(big.path, 0, HEAD_BYTES)) == HEAD_BYTES
