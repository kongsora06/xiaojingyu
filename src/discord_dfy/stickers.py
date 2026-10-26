"""A small library of sticker images the bot may send, described in stickers.json:

    [{"name": "我不知道耶", "file": "deepseek-si-kao-bu-zhi-dao.png", "when": "装傻"}]

The DM side adds to it with save_picture and sends one with a "[表情包：名字]" line; the
group brain picks one by name. Read on every use, so additions take effect without a
restart (the DM prompt lists them once per conversation).
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)
INDEX = "stickers.json"
MAX_STICKER_BYTES = 8 * 1024 * 1024


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

    def prompt_block(self) -> str:
        entries = self.entries()
        if not entries:
            return ""
        lines = "\n".join(f"- {entry['name']}：{entry['when']}" for entry in entries)
        return f"\n\n<stickers>\n{lines}\n</stickers>"
