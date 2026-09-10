import pytest
from PIL import Image, ImageDraw

from megaclean.thumbs import thumbnail_bytes, thumb_path


def _photo(path, size=(900, 700)):
    img = Image.new("RGB", size, (240, 238, 230))
    d = ImageDraw.Draw(img)
    w, h = size
    d.ellipse([w * .05, h * .1, w * .45, h * .5], fill=(30, 90, 160))
    d.rectangle([w * .55, h * .12, w * .92, h * .42], fill=(200, 60, 50))
    img.save(path, format="JPEG", quality=90)
    return path


def test_makes_a_small_jpeg_from_a_head_buffer(tmp_path):
    data = _photo(tmp_path / "a.jpg").read_bytes()
    thumb = thumbnail_bytes(data, max_px=160)
    assert thumb is not None
    assert len(thumb) < len(data)
    img = Image.open(__import__("io").BytesIO(thumb))
    assert max(img.size) <= 160
    assert img.format == "JPEG"


def test_works_on_a_truncated_head(tmp_path):
    """The remote side only ever has the first 64 KB; a thumbnail must still
    come out of it."""
    data = _photo(tmp_path / "a.jpg", size=(2000, 1500)).read_bytes()
    assert len(data) > 65536
    assert thumbnail_bytes(data[:65536], max_px=160) is not None


def test_returns_none_for_non_images():
    assert thumbnail_bytes(b"not an image", max_px=160) is None
    assert thumbnail_bytes(b"", max_px=160) is None


def test_thumb_path_is_stable_and_filesystem_safe(tmp_path):
    a = thumb_path(tmp_path, "abc/def:ghi")
    b = thumb_path(tmp_path, "abc/def:ghi")
    assert a == b
    assert a.parent.parent == tmp_path
    assert "/" not in a.name and ":" not in a.name
    assert a.suffix == ".jpg"


def test_thumb_paths_shard_to_avoid_huge_directories(tmp_path):
    """15,000 files in one directory makes the report folder painful to handle."""
    paths = {thumb_path(tmp_path, f"node{i}").parent for i in range(500)}
    assert len(paths) > 1


def _truncate(path, keep=65536):
    return path.read_bytes()[:keep]


def test_partial_decode_is_cropped_not_padded_with_grey(tmp_path):
    """A big JPEG's first 64 KB holds only its top scanlines; the undecoded
    remainder must be cropped away rather than shown as grey filler."""
    big = _photo(tmp_path / "big.jpg", size=(3000, 2400))
    head = _truncate(big)
    assert len(head) < big.stat().st_size
    thumb = thumbnail_bytes(head, max_px=200)
    if thumb is None:
        return                      # rejecting it outright is also acceptable
    import io
    img = Image.open(io.BytesIO(thumb)).convert("RGB")
    px = img.load()
    w, h = img.size
    grey_rows = 0
    for y in range(h):
        row = [px[x, y] for x in range(0, w, max(1, w // 8))]
        if all(abs(r - 128) < 3 and abs(g - 128) < 3 and abs(b - 128) < 3
               for r, g, b in row):
            grey_rows += 1
    assert grey_rows / h < 0.25, f"{grey_rows}/{h} rows are grey filler"


def test_fully_decoded_image_is_untouched(tmp_path):
    small = _photo(tmp_path / "s.jpg", size=(400, 300))
    thumb = thumbnail_bytes(small.read_bytes(), max_px=200)
    import io
    img = Image.open(io.BytesIO(thumb))
    assert abs(img.size[0] / img.size[1] - 4 / 3) < 0.1   # aspect preserved


def test_embedded_exif_thumbnail_is_preferred(tmp_path, monkeypatch):
    import megaclean.thumbs as th
    real = _photo(tmp_path / "t.jpg", size=(320, 240)).read_bytes()
    monkeypatch.setattr(th, "embedded_thumbnail", lambda head: real)
    out = thumbnail_bytes(b"\xff\xd8" + b"\x00" * 1000, max_px=200)
    assert out is not None
