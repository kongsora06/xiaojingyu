"""Images the model can look at: Discord attachments and local files, as data URLs."""

import base64
import logging
from pathlib import Path

log = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES_PER_MESSAGE = 4
SIGNATURES = {
    b"\x89PNG\r\n\x1a\n": "png",
    b"\xff\xd8\xff": "jpeg",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
}


def image_type(data: bytes) -> str | None:
    """What DeepSeek accepts (JPEG, PNG, GIF, WebP), judged by content, not by name."""
    for signature, kind in SIGNATURES.items():
        if data.startswith(signature):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def data_url(data: bytes) -> str | None:
    kind = image_type(data)
    if kind is None or len(data) > MAX_IMAGE_BYTES:
        return None
    return f"data:image/{kind};base64,{base64.b64encode(data).decode()}"


def is_image_attachment(attachment) -> bool:
    kind = (getattr(attachment, "content_type", None) or "").split("/")[0]
    size = getattr(attachment, "size", 0) or 0
    return kind == "image" and 0 < size <= MAX_IMAGE_BYTES


async def read_images(message) -> list[tuple[str, bytes]]:
    """(filename, bytes) of a Discord message's image attachments, at most a few."""
    found = []
    for attachment in getattr(message, "attachments", None) or []:
        if len(found) >= MAX_IMAGES_PER_MESSAGE or not is_image_attachment(attachment):
            continue
        try:
            data = await attachment.read()
        except Exception:
            log.warning("Could not download an image attachment")
            continue
        if image_type(data) is not None:
            found.append((attachment.filename, data))
    return found


def file_block(path) -> dict:
    """A stored DM image reference, turned into a real image block per request."""
    return {"type": "image_file", "path": str(path)}


def load_block(block: dict) -> dict:
    path = Path(block.get("path", ""))
    try:
        url = data_url(path.read_bytes())
    except OSError:
        url = None
    if url is None:
        return {"type": "text", "text": f"[图片已经不在或不是支持的格式：{path}]"}
    return {"type": "image_url", "image_url": {"url": url}}


def text_of(content) -> str:
    """Plain text of a message whose content may be a list of blocks."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        else:
            parts.append(f"[图片 {Path(block.get('path', '')).name}]")
    return "\n".join(parts)
