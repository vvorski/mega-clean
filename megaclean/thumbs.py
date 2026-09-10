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


def thumbnail_bytes(head: bytes, *, max_px: int = DEFAULT_MAX_PX,
                    quality: int = DEFAULT_QUALITY) -> bytes | None:
    """A small JPEG decoded from a (usually truncated) head buffer."""
    if not head:
        return None
    try:
        img = Image.open(io.BytesIO(head))
        img.draft("RGB", (max_px * 2, max_px * 2))   # cheap DCT downscale
        img = img.convert("RGB")
        img.thumbnail((max_px, max_px), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()
    except Exception:
        log.debug("thumbnail failed", exc_info=True)
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
