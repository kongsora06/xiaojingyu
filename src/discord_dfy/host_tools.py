"""Her own things in the owner's DMs: a notebook (the memory file), her self-portrait, a
diary, and a room.

The room (data/room/) is a folder that is hers alone: poems, lists, things she makes,
pictures she saves. Every path she gives is resolved inside it; nothing else on the
computer is reachable. Her diary lives in the room too.
"""

import asyncio
import ipaddress
import json
import re
import socket
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx2

from .images import MAX_IMAGE_BYTES, image_type
from .memory import MEMORY_SECTIONS
from .stickers import INDEX as STICKER_INDEX

MAX_OUTPUT_CHARS = 30000
MAX_SEND_BYTES = 10 * 1024 * 1024
MAX_WRITE_CHARS = 100000
MAX_LIST_ENTRIES = 200
DIARY_DIR = "diary"
PICTURE_DIR = "pictures"
MAX_REDIRECTS = 3
# A line that is only "[表情包：名字]" in a DM reply is sent as that sticker.
STICKER_LINE = re.compile(r"^\s*\[表情包[:：]\s*(.+?)\s*\]\s*$")
BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "在你的小本子（长期记忆）里记一句话，自动写在对应的一节下面、带上日期。"
                "约好的事、他特意让你记住的、你不想忘的，随手记一句就行。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要记的一句话，用第一人称"},
                    "section": {"type": "string", "enum": list(MEMORY_SECTIONS)},
                },
                "required": ["text", "section"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rewrite_self",
            "description": (
                "重写 <self>：你自己写的“我是个什么样的人”"
                "（性格、脾气、喜好、说话方式、在意的东西）。"
                "整篇换成新写的，用第一人称，2000 字以内；旧的会自动留底。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "完整的新版本"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_diary",
            "description": (
                "写一篇日记，自动记上日期和时间：今天过得怎样、心情、在想什么，写给自己看的。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_diary",
            "description": (
                "翻你以前写的日记。when 可以是某一天（2026-10-24）、某个月（2026-10），"
                "不写就是最近几篇。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"when": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "room_list",
            "description": (
                "看看你房间里（只属于你的文件夹）都有什么。"
                "path 是房间里的子文件夹，不写就是整个房间。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "room_read",
            "description": "读房间里的一个文字文件（诗、清单、你写的东西）。图片用 view_image 看。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "房间里的路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "room_write",
            "description": (
                "在房间里写一个文字文件（没有的文件夹会自动建）。append=true 接在后面写，"
                "不然就整篇换成新内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "房间里的路径，比如 诗/秋天.md"},
                    "content": {"type": "string"},
                    "append": {"type": "boolean"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_picture",
            "description": (
                "把一张图片收进房间的 pictures 文件夹。source 是网上图片的直链（http/https），"
                "或者房间里已有的图片。sticker=true 时同时加进你的表情包，when 写什么时候用它。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "name": {"type": "string", "description": "给这张图起个名字"},
                    "sticker": {"type": "boolean"},
                    "when": {"type": "string", "description": "当表情包时：什么时候用"},
                },
                "required": ["source", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": "把房间里的一个文件（图片、你写的东西，最大 10 MB）发给老爹。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "房间里的路径"}},
                "required": ["path"],
            },
        },
    },
]

WEB_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "上网搜索，返回标题、网址、日期和摘要。查新闻、最新信息、你不确定的事实时用。"
                '支持 site:、-site:、"精确短语"、lang:zh、after:2026-01-01 等写法。'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词，最多 128 字"},
                    "count": {"type": "integer", "description": "结果条数，1～10，默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "打开一个网页，把正文转成 Markdown 返回。看搜索结果的全文或者老爹发来的链接时用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
]

VIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "view_image",
        "description": (
            "看一张图片（PNG/JPEG/GIF/WebP，最大 8 MB）：房间里的图片，或者老爹以前发给你的图。"
            "图片会在这一步之后附给你。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}

PROFILE_TOOL = {
    "type": "function",
    "function": {
        "name": "update_profile",
        "description": (
            "改你自己的 Discord 资料。field=status：改状态文字（value，留空就是清掉）和在线状态"
            "（state：online 在线、idle 离开、dnd 请勿打扰），这个可以随时按心情自己改。"
            "其他 field 只有老爹在私聊里明确同意了才能改，先问他："
            "avatar / banner（value 是房间里的图片，"
            "可以先用 save_picture 存下来、view_image 看过再换）、"
            "username（全局名字，一小时最多改两次）、about（个人简介 About Me）、"
            "nickname（在群里的昵称，留空恢复默认）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "enum": ["status", "avatar", "banner", "username", "about", "nickname"],
                },
                "value": {"type": "string"},
                "state": {"type": "string", "enum": ["online", "idle", "dnd"]},
            },
            "required": ["field"],
        },
    },
}

GROUP_READ_TOOL = {
    "type": "function",
    "function": {
        "name": "group_read",
        "description": (
            "看群聊频道最近的聊天记录（最近两小时内，最多 50 条，含你自己在群里说的话）。"
            "老爹问起群里的事、或者你想知道群里刚才在聊什么时用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "频道名、频道 ID 或服务器名；只有一个群聊频道时可以省略",
                },
            },
        },
    },
}

GROUP_TOOL = {
    "type": "function",
    "function": {
        "name": "group_post",
        "description": (
            "以你自己的身份在 Discord 群聊频道里发一条消息，群里的人会看到是你说的。"
            "只在老爹让你去群里说什么、转告什么时用；你自己想去群里说话先问老爹。"
            "用你在群里说话的口吻写。之后群友接你的话，你在群里会接着聊。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要发的内容"},
                "channel": {
                    "type": "string",
                    "description": "频道名、频道 ID 或服务器名；只有一个群聊频道时可以省略",
                },
            },
            "required": ["text"],
        },
    },
}


class ToolError(Exception):
    """A tool failure reported back to the model as data."""


class Room:
    """Her folder: every path is resolved inside it, nothing outside is reachable."""

    def __init__(self, root: Path, sticker_dir: Path, image_dirs=()):
        self.root = Path(root)
        self.sticker_dir = Path(sticker_dir)
        # Other folders whose images she may look at (pictures the owner sent).
        self.image_dirs = [Path(d) for d in image_dirs]

    def resolve(self, path) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ToolError("路径不能为空")
        root = self.root.resolve()
        relative = Path(path.strip().replace("\\", "/").lstrip("/"))
        if relative.is_absolute() or relative.drive:
            raise ToolError("只能用房间里的路径")
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            raise ToolError("只能用房间里的路径")
        return target

    def image_path(self, path) -> Path:
        """A picture in the room, or one the owner sent (by its stored path)."""
        if isinstance(path, str) and path.strip():
            candidate = Path(path.strip()).resolve()
            for directory in self.image_dirs:
                directory = directory.resolve()
                if directory in candidate.parents:
                    return candidate
        return self.resolve(path)

    def show(self, path: Path) -> str:
        return path.relative_to(self.root.resolve()).as_posix() or "."

    def list(self, path="") -> str:
        base = self.resolve(path) if path else self.root.resolve()
        if not base.exists():
            return "房间还是空的" if not path else f"没有这个文件夹：{path}"
        if base.is_file():
            return f"{self.show(base)}（{base.stat().st_size} 字节）"
        entries = sorted(base.rglob("*"))
        lines = [
            f"{self.show(entry)}/" if entry.is_dir() else f"{self.show(entry)}（{size(entry)}）"
            for entry in entries[:MAX_LIST_ENTRIES]
        ]
        if len(entries) > MAX_LIST_ENTRIES:
            lines.append(f"……还有 {len(entries) - MAX_LIST_ENTRIES} 项")
        return "\n".join(lines) or "这里还是空的"

    def read(self, path) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise ToolError(f"没有这个文件：{path}")
        data = target.read_bytes()
        if image_type(data[:16]) is not None:
            return "这是一张图片，用 view_image 看"
        return clip(data.decode("utf-8", errors="replace")) or "（空的）"

    def write(self, path, content, append=False) -> str:
        if not isinstance(content, str):
            raise ToolError("content 必须是文字")
        if len(content) > MAX_WRITE_CHARS:
            raise ToolError(f"一次最多写 {MAX_WRITE_CHARS} 字")
        target = self.resolve(path)
        if target == self.root.resolve() or target.is_dir():
            raise ToolError("要写到一个文件里，不是文件夹")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a" if append is True else "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
        return f"写好了：{self.show(target)}"

    def write_diary(self, text, now: datetime) -> str:
        if not isinstance(text, str) or not text.strip():
            raise ToolError("日记是空的")
        path = self.root / DIARY_DIR / f"{now:%Y-%m}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="") as stream:
            stream.write(f"\n## {now:%Y-%m-%d %H:%M}\n{text.strip()}\n")
        return "写好了"

    def read_diary(self, when, now: datetime) -> str:
        when = str(when or "").strip()
        folder = self.root / DIARY_DIR
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", when):
            text = read_text(folder / f"{when[:7]}.md")
            days = [e for e in entries_of(text) if e.startswith(f"## {when}")]
            return clip("\n".join(days)) if days else f"{when} 没写日记"
        if re.fullmatch(r"\d{4}-\d{2}", when):
            text = read_text(folder / f"{when}.md")
            return clip(text.strip()) if text.strip() else f"{when} 没写日记"
        files = sorted(folder.glob("*.md")) if folder.is_dir() else []
        recent = [e for path in files[-2:] for e in entries_of(read_text(path))][-5:]
        return clip("\n".join(recent)) if recent else "还没写过日记"

    def check_send(self, path) -> Path:
        target = self.resolve(path)
        if not target.is_file():
            raise ToolError(f"没有这个文件：{path}")
        if target.stat().st_size > MAX_SEND_BYTES:
            raise ToolError("文件超过 10 MB，Discord 发不了")
        return target

    def keep_picture(self, data: bytes, name) -> Path:
        kind = image_type(data[:16])
        if kind is None:
            raise ToolError("这不是 PNG/JPEG/GIF/WebP 图片")
        if len(data) > MAX_IMAGE_BYTES:
            raise ToolError("图片超过 8 MB")
        stem = clean_name(name)
        folder = self.root / PICTURE_DIR
        folder.mkdir(parents=True, exist_ok=True)
        target, number = folder / f"{stem}.{kind}", 2
        while target.exists():
            target, number = folder / f"{stem}-{number}.{kind}", number + 1
        target.write_bytes(data)
        return target

    def add_sticker(self, picture: Path, name, when) -> str:
        name = " ".join(str(name or "").split())[:40]
        if not name:
            raise ToolError("表情包要有名字")
        index = self.sticker_dir / STICKER_INDEX
        try:
            entries = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            entries = []
        if not isinstance(entries, list):
            entries = []
        if any(isinstance(e, dict) and e.get("name") == name for e in entries):
            raise ToolError(f"已经有叫「{name}」的表情包了，换个名字")
        self.sticker_dir.mkdir(parents=True, exist_ok=True)
        stem, suffix = clean_name(name), picture.suffix
        file, number = self.sticker_dir / f"{stem}{suffix}", 2
        while file.exists():
            file, number = self.sticker_dir / f"{stem}-{number}{suffix}", number + 1
        file.write_bytes(picture.read_bytes())
        entries.append({"name": name, "file": file.name, "when": str(when or "").strip()[:80]})
        index.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        return name


def size(path: Path) -> str:
    n = path.stat().st_size
    return f"{n} 字节" if n < 1024 else f"{n // 1024} KB"


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def entries_of(diary: str) -> list[str]:
    return [f"## {part.strip()}" for part in diary.split("\n## ")[1:] if part.strip()]


def clean_name(name) -> str:
    stem = BAD_NAME.sub("", " ".join(str(name or "").split())).strip(" .")[:60]
    return stem or "图片"


def public_address(host: str) -> bool:
    """Only the public internet: never this computer or the local network."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addresses = {ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos}
    return bool(addresses) and all(address.is_global for address in addresses)


async def download_image(url: str, client=None) -> bytes:
    """Fetch one picture from the web (public hosts only, at most 8 MB)."""
    own = client is None
    client = client or httpx2.AsyncClient(timeout=30.0, follow_redirects=False)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(str(url or "").strip())
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                raise ToolError("只能存 http(s) 网址的图片")
            if not await asyncio.to_thread(public_address, parts.hostname):
                raise ToolError("这个网址不能访问")
            async with client.stream("GET", url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                if response.status_code != 200:
                    raise ToolError(f"图片下不下来（HTTP {response.status_code}）")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_IMAGE_BYTES:
                        raise ToolError("图片超过 8 MB")
                return bytes(data)
        raise ToolError("跳转太多次了")
    except httpx2.HTTPError as error:
        raise ToolError(f"图片下不下来（{type(error).__name__}）") from None
    finally:
        if own:
            await client.aclose()


def clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[... 省略 {len(text) - limit} 字符 ...]\n{text[-half:]}"


def what_she_did(name: str, arguments: dict) -> str | None:
    """A plain line about something she did on her own, for her later self."""
    path = str(arguments.get("path") or "").strip()
    if name == "write_diary":
        return "写了日记"
    if name == "rewrite_self":
        return "重写了 <self> 里自己写的“我”"
    if name == "room_write":
        return f"在房间里写了 {path}"
    if name == "save_picture":
        picture = str(arguments.get("name") or "").strip()
        return f"收了一张图「{picture}」" + ("，加进了表情包" if arguments.get("sticker") else "")
    if name == "remember":
        return f"在小本子上记了：{str(arguments.get('text') or '').strip()[:80]}"
    if name == "web_search":
        return f"上网搜了「{str(arguments.get('query') or '').strip()[:40]}」"
    if name == "web_fetch":
        return f"看了网页 {str(arguments.get('url') or '').strip()[:120]}"
    if name == "group_post":
        return f"在群里说了：{str(arguments.get('text') or '').strip()[:80]}"
    if name == "update_profile":
        return f"改了自己的{arguments.get('field', '')}"
    return None  # Just looking around (reading, listing, viewing) is not worth a line.


# What she writes or reads in her room and diary stays out of the monitor; he won't look.
PRIVATE_WRITES = {"write_diary", "rewrite_self"}
PRIVATE_READS = {"room_read", "read_diary"}


def monitor_text(name: str, output: str) -> str:
    """A tool result as the Live Monitor shows it."""
    if name in PRIVATE_READS and not output.startswith("错误"):
        return f"（她房间里的东西，不显示，{len(output)} 字）"
    return output


def describe_call(name: str, arguments: dict) -> str:
    """One-line human summary of a tool call for the monitor and the DM trail."""
    if name == "group_post":
        detail = f"{arguments.get('channel') or ''} {arguments.get('text', '')}"
    elif name == "update_profile":
        detail = f"{arguments.get('field', '')} {arguments.get('value') or ''}"
    elif name == "web_search":
        detail = arguments.get("query", "")
    elif name in {"web_fetch", "save_picture"}:
        detail = arguments.get("url") or arguments.get("source", "")
    elif name in {"room_read", "room_write", "room_list", "send_file", "view_image"}:
        detail = arguments.get("path", "")
    elif name == "remember":
        detail = f"{arguments.get('section', '')} {arguments.get('text', '')}"
    elif name in PRIVATE_WRITES:
        detail = ""  # Her diary is hers; her self-portrait is shown by !self.
    else:
        detail = json.dumps(arguments, ensure_ascii=False)
    detail = " ".join(str(detail).split()).replace("`", "'")
    if len(detail) > 160:
        detail = detail[:157] + "..."
    return f"`{name}` {detail}".rstrip()
