"""Client-side content identity.

MEGA exposes no content hash, so identity is derived from bytes we fetch
ourselves. The exact signature reads only the first and last 64 KB, which is
enough to separate photos that merely share a byte size.
"""
from __future__ import annotations

import hashlib

HEAD_BYTES = 65536
TAIL_BYTES = 65536


def needs_tail_read(size: int) -> bool:
    """True when a separate tail read would cover bytes the head read misses."""
    return size > HEAD_BYTES + TAIL_BYTES


def content_signature(size: int, head: bytes, tail: bytes) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update(size.to_bytes(8, "little"))
    h.update(len(head).to_bytes(4, "little"))
    h.update(head)
    h.update(tail)
    return h.hexdigest()


def signature_of_bytes(data: bytes) -> str:
    """Signature for a file whose full contents are already in memory."""
    size = len(data)
    if needs_tail_read(size):
        return content_signature(size, data[:HEAD_BYTES], data[-TAIL_BYTES:])
    return content_signature(size, data, b"")
