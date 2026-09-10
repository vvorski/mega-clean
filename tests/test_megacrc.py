import zlib

from megaclean.megacrc import mega_crc, mega_crc_bytes


def _quarters_be(buf):
    """Independent formulation: per-quarter CRC32, big-endian words."""
    n = len(buf)
    return b"".join(zlib.crc32(buf[i * n // 4:(i + 1) * n // 4]).to_bytes(4, "big")
                    for i in range(4))


def test_tiny_file_is_its_own_zero_padded_content():
    assert mega_crc_bytes(b"abc") == b"abc" + b"\0" * 13
    assert mega_crc_bytes(b"x" * 16) == b"x" * 16


def test_small_file_uses_full_quarter_crcs():
    buf = bytes(range(256)) * 10          # 2560 bytes, <= 8192
    assert mega_crc_bytes(buf) == _quarters_be(buf)


def test_at_8192_bytes_the_sparse_blocks_tile_the_file_exactly():
    """32 blocks of 64 bytes per word over (size-64)/127 steps land on every
    64-byte boundary of an 8192-byte file, so sparse == full there. This is
    the same equivalence observed against the real account."""
    buf = bytes((i * 31) % 256 for i in range(8192))
    assert mega_crc_bytes(buf) == _quarters_be(buf)


def test_large_file_only_samples_so_a_change_in_an_unsampled_byte_is_invisible():
    """Documents the 1-in-80 false-positive mechanism rather than hiding it."""
    buf = bytearray(bytes((i * 7) % 256 for i in range(100_000)))
    a = mega_crc_bytes(bytes(buf))
    buf[70_001] ^= 0xFF                    # between sampled blocks
    assert mega_crc_bytes(bytes(buf)) == a


def test_large_file_change_inside_a_sampled_block_is_visible():
    buf = bytearray(bytes((i * 7) % 256 for i in range(100_000)))
    a = mega_crc_bytes(bytes(buf))
    buf[0] ^= 0xFF                         # first block is always sampled
    assert mega_crc_bytes(bytes(buf)) != a


def test_file_path_variant_matches_bytes_variant(tmp_path):
    buf = bytes((i * 13) % 256 for i in range(50_000))
    p = tmp_path / "f.bin"
    p.write_bytes(buf)
    assert mega_crc(p) == mega_crc_bytes(buf)
    assert len(mega_crc(p)) == 16
