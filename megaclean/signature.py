"""Client-side content identity.

MEGA exposes no content hash, so identity is derived from bytes we fetch
ourselves. The exact signature reads only the first and last 64 KB, which is
enough to separate photos that merely share a byte size.
"""
from __future__ import annotations

import hashlib
import io
import logging

import exifread
import imagehash
import pillow_heif
from PIL import Image, ImageFile

pillow_heif.register_heif_opener()
# A 64 KB head buffer is a truncated image by construction; decoding it is
# expected and the partial result is good enough for a perceptual hash.
ImageFile.LOAD_TRUNCATED_IMAGES = True

log = logging.getLogger(__name__)

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

_EXIF_FIELDS = (
    "Image Make",
    "Image Model",
    "EXIF DateTimeOriginal",
    "EXIF SubSecTimeOriginal",
    "EXIF ExposureTime",
    "EXIF FNumber",
)


def _read_exif(head: bytes) -> dict:
    """Parse EXIF from a possibly-truncated buffer.

    exifread tolerates truncation because the APP1 segment sits at the front of
    the file, which is what makes a 64 KB range read sufficient.
    """
    try:
        return exifread.process_file(io.BytesIO(head), details=True,
                                     strict=False)
    except Exception:                       # exifread raises assorted types
        log.debug("EXIF parse failed", exc_info=True)
        return {}


def exif_key(head: bytes) -> str | None:
    """A grouping key for "the same shot", or None if it cannot be determined.

    DateTimeOriginal is required: without it the remaining fields are far too
    weak to group by. Exposure and sub-second time are included so that burst
    frames sharing a one-second timestamp stay apart.
    """
    tags = _read_exif(head)
    if not tags:
        return None
    dt = tags.get("EXIF DateTimeOriginal")
    if dt is None or not str(dt).strip():
        return None
    return "|".join(str(tags.get(f, "")).strip() for f in _EXIF_FIELDS)


def embedded_thumbnail(head: bytes) -> bytes | None:
    """The camera-embedded JPEG thumbnail, when the file carries one."""
    tags = _read_exif(head)
    thumb = tags.get("JPEGThumbnail")
    if isinstance(thumb, (bytes, bytearray)) and len(thumb) > 0:
        return bytes(thumb)
    return None


def _open(data: bytes) -> "Image.Image | None":
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img.convert("RGB")
    except Exception:
        log.debug("image decode failed", exc_info=True)
        return None


def perceptual_hash(head: bytes) -> tuple[str, str] | None:
    """Return (hex_hash, provenance).

    The camera-embedded thumbnail is preferred because it is fully present in
    the head buffer, whereas the main image is truncated. Hashes must only be
    compared within the same provenance class.
    """
    thumb = embedded_thumbnail(head)
    if thumb:
        img = _open(thumb)
        if img is not None:
            return str(imagehash.phash(img)), "thumb"
    img = _open(head)
    if img is not None:
        return str(imagehash.phash(img)), "full"
    return None


def image_dimensions(head: bytes) -> tuple[int, int] | None:
    try:
        with Image.open(io.BytesIO(head)) as img:
            return img.size
    except Exception:
        return None


def hamming_hex(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")
