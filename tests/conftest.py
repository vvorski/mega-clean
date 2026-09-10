"""Fixture images are generated at test time; no binaries are committed."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image


def _exif_bytes(make: str, model: str, dt: str, subsec: str = "") -> bytes:
    """Build a minimal EXIF block using Pillow's own Exif container."""
    exif = Image.Exif()
    exif[0x010F] = make                      # Make
    exif[0x0110] = model                     # Model
    ifd = {0x9003: dt, 0x829A: (1, 200), 0x829D: (28, 10)}
    if subsec:
        ifd[0x9291] = subsec                 # SubSecTimeOriginal
    exif[0x8769] = ifd                       # Exif IFD pointer
    return exif.tobytes()


@pytest.fixture
def make_jpeg():
    def _make(path: Path, *, size=(64, 48), color=(255, 0, 0),
              make="Canon", model="EOS R5", dt="2024:05:01 12:00:00",
              subsec="", quality=90, with_exif=True) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", size, color)
        # Give the image real structure so perceptual hashing has signal.
        for x in range(0, size[0], 8):
            for y in range(0, size[1], 8):
                if (x // 8 + y // 8) % 2 == 0:
                    img.paste((0, 0, 255), (x, y, min(x + 8, size[0]),
                                            min(y + 8, size[1])))
        kwargs = {"quality": quality}
        if with_exif:
            kwargs["exif"] = _exif_bytes(make, model, dt, subsec)
        img.save(path, format="JPEG", **kwargs)
        return path
    return _make
