from PIL import Image, ImageDraw

from megaclean.signature import (
    hamming_hex, image_dimensions, perceptual_hash,
)


def _photo(path, size=(512, 512), quality=90):
    """A structured, low-frequency image.

    Perceptual hashing is unstable on near-uniform images (a plain gradient
    leaves every DCT coefficient hovering at the median, so bits flip at
    random). Real photographs have structure; the fixture must too.
    """
    img = Image.new("RGB", size, (240, 238, 230))
    d = ImageDraw.Draw(img)
    w, h = size
    d.ellipse([w * 0.05, h * 0.10, w * 0.45, h * 0.50], fill=(30, 90, 160))
    d.rectangle([w * 0.55, h * 0.12, w * 0.92, h * 0.42], fill=(200, 60, 50))
    d.ellipse([w * 0.30, h * 0.55, w * 0.70, h * 0.92], fill=(60, 150, 80))
    d.rectangle([w * 0.05, h * 0.62, w * 0.22, h * 0.95], fill=(20, 20, 25))
    img.save(path, format="JPEG", quality=quality)
    return path


def test_hash_has_provenance(make_jpeg, tmp_path):
    p = make_jpeg(tmp_path / "a.jpg", size=(256, 256))
    result = perceptual_hash(p.read_bytes())
    assert result is not None
    _, src = result
    assert src in ("thumb", "full")


def test_embedded_thumbnail_is_preferred_when_present(make_jpeg, tmp_path,
                                                      monkeypatch):
    """Cameras embed a thumbnail; it is fully present in the head buffer while
    the main image is truncated, so it wins."""
    import megaclean.signature as sig

    thumb = tmp_path / "t.jpg"
    _photo(thumb, size=(160, 120))
    monkeypatch.setattr(sig, "embedded_thumbnail",
                        lambda head: thumb.read_bytes())
    p = make_jpeg(tmp_path / "a.jpg", size=(256, 256))
    _, src = sig.perceptual_hash(p.read_bytes())
    assert src == "thumb"


def test_resized_copy_hashes_close(tmp_path):
    a = _photo(tmp_path / "a.jpg", size=(512, 512))
    img = Image.open(a).resize((256, 256))
    b = tmp_path / "b.jpg"
    img.save(b, format="JPEG", quality=85)
    ha, sa = perceptual_hash(a.read_bytes())
    hb, sb = perceptual_hash(b.read_bytes())
    assert sa == sb
    assert hamming_hex(ha, hb) <= 6


def test_re_encoded_copy_hashes_close(tmp_path):
    a = _photo(tmp_path / "a.jpg")
    img = Image.open(a)
    b = tmp_path / "b.jpg"
    img.save(b, format="JPEG", quality=25)
    ha, _ = perceptual_hash(a.read_bytes())
    hb, _ = perceptual_hash(b.read_bytes())
    assert hamming_hex(ha, hb) <= 6


def test_different_images_hash_far(tmp_path):
    a = _photo(tmp_path / "a.jpg", size=(256, 256))
    b = tmp_path / "b.jpg"
    Image.effect_noise((256, 256), 96).convert("RGB").save(b, format="JPEG")
    ha, _ = perceptual_hash(a.read_bytes())
    hb, _ = perceptual_hash(b.read_bytes())
    assert hamming_hex(ha, hb) > 6


def test_dimensions_read_from_head(make_jpeg, tmp_path):
    p = make_jpeg(tmp_path / "a.jpg", size=(320, 240))
    assert image_dimensions(p.read_bytes()) == (320, 240)


def test_garbage_returns_none():
    assert perceptual_hash(b"nonsense") is None
    assert image_dimensions(b"nonsense") is None


def test_hamming_hex_basics():
    assert hamming_hex("00", "00") == 0
    assert hamming_hex("00", "01") == 1
    assert hamming_hex("0f", "00") == 4
