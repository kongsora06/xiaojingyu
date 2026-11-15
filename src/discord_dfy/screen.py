"""The owner's main screen, captured only while he streams it, as small JPEGs.

Nothing is written to disk: a frame lives in memory until the next one replaces it.
"""

import io
from dataclasses import dataclass

from PIL import Image

# Wide enough to read a game HUD or a web page, small enough to keep image tokens low.
WIDTH = 1280
QUALITY = 70
# A tiny grayscale copy, compared between frames to see whether anything changed.
THUMB = (32, 18)


@dataclass(frozen=True, slots=True)
class Frame:
    jpeg: bytes
    thumb: bytes


def primary_monitor(monitors: list[dict]) -> dict:
    """mss lists every screen together first, then each one; Windows' main screen is marked
    is_primary, and its top-left corner is always (0, 0)."""
    for monitor in monitors[1:]:
        if monitor.get("is_primary"):
            return monitor
    for monitor in monitors[1:]:
        if monitor["left"] == 0 and monitor["top"] == 0:
            return monitor
    return monitors[1] if len(monitors) > 1 else monitors[0]


def encode(image: Image.Image) -> Frame:
    image = image.convert("RGB")
    if image.width > WIDTH:
        height = max(1, round(image.height * WIDTH / image.width))
        image = image.resize((WIDTH, height), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=QUALITY)
    thumb = image.convert("L").resize(THUMB, Image.Resampling.BILINEAR).tobytes()
    return Frame(buffer.getvalue(), thumb)


def grab_primary() -> Frame:
    """One shot of the main screen (blocking: run it in a thread)."""
    import mss

    with mss.MSS() as screens:
        shot = screens.grab(primary_monitor(screens.monitors))
    return encode(Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX"))


def difference(a: bytes, b: bytes) -> float:
    """How different two thumbnails are, from 0 (same) to 1."""
    if not a or len(a) != len(b):
        return 1.0
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / (255 * len(a))
