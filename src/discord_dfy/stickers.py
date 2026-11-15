"""A small library of sticker images the bot may send, described in stickers.json:

    [{"name": "我不知道耶", "file": "deepseek-si-kao-bu-zhi-dao.png", "when": "装傻"}]

The DM side adds to it with save_picture and sends one with a "[表情包：名字]" line; the
group brain picks one by name. Read on every use, so additions take effect without a
restart (the DM prompt lists them once per conversation).
"""

import json
import logging
import re
from pathlib import Path

from .images import image_type

log = logging.getLogger(__name__)
# A line that is only "[表情包：名字]" is sent as that sticker (DM replies and group lines).
STICKER_LINE = re.compile(r"^\s*\[表情包[:：]\s*(.+?)\s*\]\s*$")
# She sometimes writes just "[哼]" or "【哼】"; that counts only when it names a real sticker.
BARE_LINE = re.compile(r"^\s*[\[【]\s*(.+?)\s*[\]】]\s*$")
INDEX = "stickers.json"
MAX_STICKER_BYTES = 8 * 1024 * 1024


class StickerError(ValueError):
    """Why a picture could not be kept as a sticker, in words for her."""


class StickerBook:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def entries(self) -> list[dict]:
        try:
            data = json.loads((self.directory / INDEX).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        found = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            name, file = str(item.get("name") or "").strip(), str(item.get("file") or "")
            path = self.directory / Path(file).name  # Only files inside the library.
            if name and path.is_file() and path.stat().st_size <= MAX_STICKER_BYTES:
                found.append({"name": name, "path": path, "when": str(item.get("when") or "")})
        return found

    def find(self, name: str | None) -> dict | None:
        if not name:
            return None
        name = name.strip()
        return next((entry for entry in self.entries() if entry["name"] == name), None)

    def take_bare(self, lines) -> tuple[tuple[str, ...], list[str]]:
        """Split off lines that are only a known sticker's name in brackets."""
        names = {entry["name"] for entry in self.entries()}
        kept, found = [], []
        for line in lines:
            match = BARE_LINE.match(line)
            if match and match.group(1) in names:
                found.append(match.group(1))
            else:
                kept.append(line)
        return tuple(kept), found

    def add(self, data: bytes, name, when) -> str:
        """Keep a picture as a sticker under a new name; the name it got."""
        kind = image_type(data[:16])
        if kind is None:
            raise StickerError("这不是 PNG/JPEG/GIF/WebP 图片")
        if len(data) > MAX_STICKER_BYTES:
            raise StickerError("图片超过 8 MB")
        name = " ".join(str(name or "").split())[:40]
        if not name:
            raise StickerError("表情包要有名字")
        index = self.directory / INDEX
        try:
            entries = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            entries = []
        if not isinstance(entries, list):
            entries = []
        if any(isinstance(e, dict) and e.get("name") == name for e in entries):
            raise StickerError(f"已经有叫「{name}」的表情包了，换个名字")
        for entry in self.entries():
            path = entry["path"]
            if path.stat().st_size == len(data) and path.read_bytes() == data:
                raise StickerError(f"这张已经在表情包里了，叫「{entry['name']}」")
        self.directory.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r'[\\/:*?"<>|\s]+', "-", name).strip("-.") or "sticker"
        file, number = self.directory / f"{stem}.{kind}", 2
        while file.exists():
            file, number = self.directory / f"{stem}-{number}.{kind}", number + 1
        file.write_bytes(data)
        entries.append({"name": name, "file": file.name, "when": str(when or "").strip()[:80]})
        index.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Sticker added: %s", file.name)
        return name

    def prompt_block(self) -> str:
        entries = self.entries()
        if not entries:
            return ""
        lines = "\n".join(f"- {entry['name']}：{entry['when']}" for entry in entries)
        return f"\n\n<stickers>\n{lines}\n</stickers>"
