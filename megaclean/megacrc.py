"""MEGA's file fingerprint CRC, computed locally.

Reproduces the 16-byte CRC that MEGA's clients store in every file node's
`c` attribute, so a local file can be looked up against the whole account
without fetching anything. Constants were calibrated against files whose
stored CRC was known: 64-byte blocks, 32 per word, offsets spread over
size-64, words big-endian. Files of 8192 bytes or less are CRC'd in four full
quarters; 16 bytes or less are stored verbatim, zero-padded.

It is a sparse sample, not a hash: on the calibration account it matched the
bytes 79 times in 80. Treat a match as a strong candidate and confirm before
acting.
"""
from __future__ import annotations

import zlib
from pathlib import Path

CRC_SIZE = 64
BLOCKS = 32
MAX_FULL = 8192
WORDS = 4


def _offsets(size: int) -> list[list[int]]:
    return [[(size - CRC_SIZE) * (i * BLOCKS + j) // (WORDS * BLOCKS - 1)
             for j in range(BLOCKS)] for i in range(WORDS)]


def mega_crc_bytes(data: bytes) -> bytes:
    size = len(data)
    if size <= 16:
        return data + b"\0" * (16 - size)
    if size <= MAX_FULL:
        return b"".join(
            zlib.crc32(data[i * size // WORDS:(i + 1) * size // WORDS]).to_bytes(4, "big")
            for i in range(WORDS))
    out = b""
    for word in _offsets(size):
        c = 0
        for off in word:
            c = zlib.crc32(data[off:off + CRC_SIZE], c)
        out += c.to_bytes(4, "big")
    return out


def mega_crc(path: Path) -> bytes:
    """Same result as `mega_crc_bytes`, reading only the sampled blocks."""
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size <= MAX_FULL:
            return mega_crc_bytes(fh.read())
        out = b""
        for word in _offsets(size):
            c = 0
            for off in word:
                fh.seek(off)
                c = zlib.crc32(fh.read(CRC_SIZE), c)
            out += c.to_bytes(4, "big")
        return out
