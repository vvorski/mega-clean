import pytest
from megaclean.signature import (
    HEAD_BYTES, TAIL_BYTES, content_signature, signature_of_bytes,
    needs_tail_read,
)


def test_signature_is_stable_and_hex():
    sig = content_signature(1000, b"head", b"tail")
    assert sig == content_signature(1000, b"head", b"tail")
    assert len(sig) == 32
    int(sig, 16)


def test_size_participates_in_the_signature():
    assert content_signature(1000, b"h", b"t") != content_signature(1001, b"h", b"t")


def test_head_and_tail_are_not_interchangeable():
    assert content_signature(10, b"a", b"b") != content_signature(10, b"b", b"a")


def test_small_files_need_no_tail_read():
    assert needs_tail_read(HEAD_BYTES + TAIL_BYTES + 1) is True
    assert needs_tail_read(HEAD_BYTES + TAIL_BYTES) is False
    assert needs_tail_read(10) is False


def test_whole_file_signature_agrees_with_ranged_signature():
    data = bytes(range(256)) * 1024
    ranged = content_signature(len(data), data[:HEAD_BYTES], data[-TAIL_BYTES:])
    assert signature_of_bytes(data) == ranged


def test_small_whole_file_agrees_with_overlapping_range():
    data = b"tiny file contents"
    ranged = content_signature(len(data), data, b"")
    assert signature_of_bytes(data) == ranged


def test_different_content_same_size_differs():
    a = b"A" * 200000
    b = b"A" * 199999 + b"B"
    assert signature_of_bytes(a) != signature_of_bytes(b)
