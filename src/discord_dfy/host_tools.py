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
from .journal import JOURNAL_SECTION
from .memory import MEMORY_SECTIONS
from .stickers import StickerBook, StickerError

MAX_OUTPUT_CHARS = 30000
MAX_SEND_BYTES = 10 * 1024 * 1024
MAX_WRITE_CHARS = 100000
MAX_LIST_ENTRIES = 200
DIARY_DIR = "diary"
PICTURE_DIR = "pictures"
MAX_REDIRECTS = 3
# The showcase: the few things in her room she has put out for him to open.
MAX_SHOWN = 24
MAX_NOTE_CHARS = 200
BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "在小本子上记一句，记具体发生的事和对话中实际表达过的感受，不补写动机或情绪。"
                "今天发生的事选「今天」，记在今天这页、带上时间；"
                "觉得以后可能要紧，就把 important 打上。"
                "关于主人、你们之间、你自己、群里谁的长久的事，选对应的那一节。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要记的一句话，用第一人称"},
                    "section": {
                        "type": "string",
                        "enum": [JOURNAL_SECTION, *MEMORY_SECTIONS],
                    },
                    "important": {"type": "boolean"},
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
                "（性格、脾气、喜好、说话方式、跟主人怎么相处、在意的东西）。"
                "是 persona 相处基调的具体展开；有持续的新认识再改，一次嘴硬或拒绝不定成稳定性格。"
                "整篇换成新写的，用第一人称，4000 字以内；旧的会自动留底。"
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
                "篇幅随内容，可以短记也可以长篇，不受日常闲聊简短的要求限制。"
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
            "name": "recall",
            "description": (
                "翻你和主人以前私聊的原话，!new 之前的旧对话也在。"
                "when 是某一天（2026-10-24）或某个月（2026-10），query 是关键词；至少给一个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"when": {"type": "string"}, "query": {"type": "string"}},
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
                "可以自由写故事、信、随笔、诗等，题材和形式自定，长篇也可以，不必压缩成聊天短句。"
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
            "name": "room_show",
            "description": (
                "把房间里的一样东西（你写的一篇东西，或一张图）摆进橱窗，或者从橱窗收回去。"
                "橱窗是房间里唯一主人能打开看的地方；摆进去的是那样东西本身，你之后再改它，他看到的也是改过的。"
                "note 是你想贴在旁边的一句话，可以不写。show=false 是收回去。日记不能摆。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "房间里的路径"},
                    "note": {"type": "string", "description": "贴在旁边的一句话"},
                    "show": {"type": "boolean", "description": "不写就是摆进去"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_picture",
            "description": (
                "把一张图片收进房间的 pictures 文件夹。source 是网上图片的直链（http/https）、"
                "房间里已有的图片，或者主人发给你的图（写图旁边标的名字）。"
                "sticker=true 时同时加进你的表情包，when 写什么时候用它。"
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
            "description": (
                "把房间里的一个文件（图片、你写的东西，最大 10 MB）发给主人，或者发到群里。"
                "发到群里和 group_post 一样，只在主人让你发的时候用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "房间里的路径"},
                    "to": {
                        "type": "string",
                        "enum": ["主人", "群里"],
                        "description": "发给谁，不填就是发给主人",
                    },
                    "channel": {
                        "type": "string",
                        "description": "发到群里时用：频道名、频道 ID 或服务器名；只有一个群聊频道时可以省略",
                    },
                },
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
                "打开一个网页，把正文转成 Markdown 返回。看搜索结果的全文或者主人发来的链接时用。"
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
            "看一张图片（PNG/JPEG/GIF/WebP，最大 8 MB）：房间里的图片，或者主人以前发给你的图。"
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
            "其他 field 只有主人在私聊里明确同意了才能改，先问他："
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
            "主人问起群里的事、或者你想知道群里刚才在聊什么时用。"
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
            "只在主人让你去群里说什么、转告什么时用；你自己想去群里说话先问主人。"
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
        """A picture in the room, or one the owner sent (by its stored path, or just the
        file name it was shown to her with)."""
        if isinstance(path, str) and path.strip():
            candidate = Path(path.strip()).resolve()
            for directory in self.image_dirs:
                directory = directory.resolve()
                if directory in candidate.parents:
                    return candidate
            bare = path.strip()
            if Path(bare).name == bare and not self.resolve(bare).is_file():
                for directory in self.image_dirs:
                    if (sent := directory.resolve() / bare).is_file():
                        return sent
        return self.resolve(path)

    def show(self, path: Path) -> str:
        # The room may be configured as a relative path; compare both resolved.
        return path.resolve().relative_to(self.root.resolve()).as_posix() or "."

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
        if not path and (shown := read_showcase(self.root)):
            lines.append("橱窗里摆着：" + "、".join(item["path"] for item in shown))
        return "\n".join(lines) or "这里还是空的"

    def display(self, path, note, now: datetime) -> str:
        """Put one of her things in the showcase, where he may open it."""
        target = self.resolve(path)
        if not target.is_file():
            raise ToolError(f"要摆一样具体的东西，房间里没有这个文件：{path}")
        shown = self.show(target)
        if shown.split("/")[0] == DIARY_DIR:
            raise ToolError("日记不摆橱窗。想给他看哪一篇，抄一份出来再摆，或者直接发给他")
        note = " ".join(str(note or "").split())[:MAX_NOTE_CHARS]
        items = [item for item in read_showcase(self.root) if item["path"] != shown]
        if len(items) >= MAX_SHOWN:
            raise ToolError(f"橱窗摆满了（{MAX_SHOWN} 样），先收回去一样")
        items.append({"path": shown, "note": note, "since": now.isoformat(timespec="seconds")})
        write_showcase(self.root, items)
        return f"摆进橱窗了：{shown}"

    def put_away(self, path) -> str:
        shown = self.show(self.resolve(path))
        items = read_showcase(self.root)
        kept = [item for item in items if item["path"] != shown]
        if len(kept) == len(items):
            return f"橱窗里本来就没有：{shown}"
        write_showcase(self.root, kept)
        return f"从橱窗收回来了：{shown}"

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

    def last_diary(self) -> str | None:
        """Her latest entry, heading included, or None before she has written one."""
        folder = self.root / DIARY_DIR
        for path in sorted(folder.glob("*.md"), reverse=True) if folder.is_dir() else []:
            if entries := entries_of(read_text(path)):
                return entries[-1]
        return None

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
        try:
            return StickerBook(self.sticker_dir).add(picture.read_bytes(), name, when)
        except StickerError as error:
            raise ToolError(str(error)) from error


def showcase_path(room_dir) -> Path:
    """The list of what she has put out. It sits next to the room, not in it, so nothing
    she writes in the room can change what he is allowed to open."""
    room_dir = Path(room_dir)
    return room_dir.with_name(f"{room_dir.name}-showcase.json")


def read_showcase(room_dir) -> list[dict]:
    """[{"path": "诗/秋天.md", "note": "...", "since": "2026-11-10T12:00:00+08:00"}]"""
    try:
        data = json.loads(showcase_path(room_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [
        {
            "path": item["path"],
            "note": str(item.get("note") or ""),
            "since": str(item.get("since") or ""),
        }
        for item in data
        if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"]
    ]


def write_showcase(room_dir, items: list[dict]) -> None:
    path = showcase_path(room_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


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
    if name == "room_show":
        if arguments.get("show") is False:
            return f"把 {path} 从橱窗收回来了"
        return f"把 {path} 摆进了橱窗"
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
    if name == "send_file" and to_group(arguments):
        return f"在群里发了 {path}"
    if name == "update_profile":
        return f"改了自己的{arguments.get('field', '')}"
    return None  # Just looking around (reading, listing, viewing) is not worth a line.


def to_group(arguments: dict) -> bool:
    """send_file aimed at a group channel rather than at him."""
    return str(arguments.get("to") or "").strip() == "群里"


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
    elif name == "send_file" and to_group(arguments):
        detail = f"群里 {arguments.get('channel') or ''} {arguments.get('path', '')}"
    elif name == "room_show":
        detail = (
            f"{'收回' if arguments.get('show') is False else '摆出'} {arguments.get('path', '')}"
        )
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
