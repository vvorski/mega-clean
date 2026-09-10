"""Thumbnails for the duplicate gallery, made from bytes we already fetched.

Verification reads the first 64 KB of every candidate file. That buffer is
enough to decode a preview, so the gallery costs no extra transfer.
"""
from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path

from PIL import Image, ImageFile

from .signature import embedded_thumbnail

# Head buffers are truncated images by construction, so decoding one is the
# normal case here, not an error. Set explicitly rather than relying on another
# module having been imported first.
ImageFile.LOAD_TRUNCATED_IMAGES = True
# Pillow's decompression-bomb guard assumes untrusted input. These are the
# user's own photos and panoramas legitimately run to nine figures of pixels,
# so raise the ceiling rather than skip them -- but keep a ceiling.
Image.MAX_IMAGE_PIXELS = 500_000_000

log = logging.getLogger(__name__)

DEFAULT_MAX_PX = 200
DEFAULT_QUALITY = 72


GREY = 128
MIN_DECODED_FRACTION = 0.25


def _crop_undecoded(img: Image.Image) -> Image.Image | None:
    """Trim the grey filler Pillow leaves where a truncated JPEG ran out.

    A 64 KB prefix of a multi-megabyte photo contains only its top scanlines;
    everything below decodes as flat grey. Showing that as a preview would be
    misleading, so keep the part that is real and drop the rest.
    """
    w, h = img.size
    px = img.load()
    step = max(1, w // 10)
    last_real = -1
    for y in range(h):
        for x in range(0, w, step):
            r, g, b = px[x, y][:3]
            if abs(r - GREY) > 2 or abs(g - GREY) > 2 or abs(b - GREY) > 2:
                last_real = y
                break
    if last_real < 0:
        return None
    if last_real >= h - 2:
        return img                          # nothing to trim
    if (last_real + 1) / h < MIN_DECODED_FRACTION:
        return None                         # too little of the photo to be useful
    return img.crop((0, 0, w, last_real + 1))


def thumbnail_bytes(head: bytes, *, max_px: int = DEFAULT_MAX_PX,
                    quality: int = DEFAULT_QUALITY) -> bytes | None:
    """A small JPEG preview built from a (usually truncated) head buffer.

    The camera's embedded EXIF thumbnail is preferred when present: it is
    complete inside the head buffer, whereas the main image is not.
    """
    if not head:
        return None
    source = embedded_thumbnail(head) or head
    try:
        img = Image.open(io.BytesIO(source))
        img.draft("RGB", (max_px * 2, max_px * 2))   # cheap DCT downscale
        img = img.convert("RGB")
    except Exception:
        log.debug("thumbnail decode failed", exc_info=True)
        return None
    trimmed = _crop_undecoded(img)
    if trimmed is None:
        return None
    try:
        trimmed.thumbnail((max_px, max_px), Image.LANCZOS)
        out = io.BytesIO()
        trimmed.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()
    except Exception:
        log.debug("thumbnail encode failed", exc_info=True)
        return None


def thumb_path(root: Path, node_id: str) -> Path:
    """Sharded, filesystem-safe path for one node's thumbnail."""
    digest = hashlib.sha1(node_id.encode("utf-8")).hexdigest()
    return Path(root) / digest[:2] / f"{digest}.jpg"


def write_thumbnail(root: Path, node_id: str, head: bytes, **kwargs) -> bool:
    data = thumbnail_bytes(head, **kwargs)
    if data is None:
        return False
    path = thumb_path(root, node_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True
