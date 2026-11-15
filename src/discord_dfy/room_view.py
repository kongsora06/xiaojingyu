"""Her room, drawn: a local page for the owner (dfy-room). It only reads.

From outside he sees the shape of the room: what stands on which shelf by name, how thick
each thing is, when it was last touched, which days she wrote in her diary. Nothing opens
except what she has put in the showcase herself (room_show). A second page shows how her
self page changed from one rewrite to the next, which he can already read with !self.

Served on 127.0.0.1 only. No agent control channel; nothing is ever written.
"""

import argparse
import json
import os
import re
import threading
import webbrowser
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dotenv import dotenv_values

from .host_tools import DIARY_DIR, PICTURE_DIR, Room, ToolError, read_showcase
from .images import MAX_IMAGE_BYTES, image_type
from .now import Watcher
from .portrait import HISTORY_DIR, read_self, self_path

PICTURE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
TEXT_SUFFIXES = {".md", ".txt"}
# Bigger texts are not counted (their thickness comes from bytes) or opened.
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 2000
DIARY_HEADING = re.compile(r"^## (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})", re.MULTILINE)
HISTORY_NAME = re.compile(r"self-(\d{8}-\d{6})\.md")
# A sentence with its closing marks, a run without any, or a line break.
SENTENCE = re.compile(r"[^。！？!?\n]*[。！？!?]+[”’」』）)]*|[^。！？!?\n]+|\n+")
DEFAULT_PORT = 8765


class NotShown(Exception):
    """He asked for something she has not put out."""


def stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds).astimezone().isoformat(timespec="seconds")


def kind_of(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in PICTURE_SUFFIXES:
        return "picture"
    return "text" if suffix in TEXT_SUFFIXES else "other"


def describe(entry: Path, root: Path, shown: set[str]) -> dict:
    """One thing as seen from outside: its name and size, never what is in it."""
    info = entry.stat()
    kind = kind_of(entry)
    chars = None
    if kind == "text" and info.st_size <= MAX_TEXT_BYTES:
        try:
            chars = len(entry.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    path = entry.relative_to(root).as_posix()
    return {
        "name": entry.stem,
        "path": path,
        "kind": kind,
        "bytes": info.st_size,
        "chars": chars,
        "modified": stamp(info.st_mtime),
        "shown": path in shown,
    }


def diary_outline(folder: Path) -> dict:
    """How many entries, on which days, and the last time: the cover, not the pages."""
    days: Counter[str] = Counter()
    last, size = None, 0
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        size += len(text)
        for day, clock in DIARY_HEADING.findall(text):
            days[day] += 1
            last = max(last or "", f"{day}T{clock}")
    return {
        "entries": sum(days.values()),
        "days": dict(sorted(days.items())),
        "last": last,
        "chars": size,
    }


def scan_room(room_dir) -> dict:
    root = Path(room_dir).resolve()
    showcase = read_showcase(room_dir)
    shown = {item["path"] for item in showcase}
    shelves: dict[str, list[dict]] = {}
    loose, pictures, by_path = [], [], {}
    entries = sorted(root.rglob("*"))[:MAX_ENTRIES] if root.is_dir() else []
    for entry in entries:
        relative = entry.relative_to(root)
        if entry.is_symlink() or any(part.startswith(".") for part in relative.parts):
            continue
        top = relative.parts[0]
        if top == DIARY_DIR:
            continue
        if entry.is_dir():
            if top != PICTURE_DIR:
                shelves.setdefault(relative.as_posix(), [])
            continue
        try:
            thing = describe(entry, root, shown)
        except OSError:
            continue
        by_path[thing["path"]] = thing
        if len(relative.parts) == 1:
            loose.append(thing)
        elif top == PICTURE_DIR:
            pictures.append(thing)
        else:
            shelves.setdefault(relative.parent.as_posix(), []).append(thing)
    for things in (*shelves.values(), loose, pictures):
        things.sort(key=lambda thing: thing["modified"])
    return {
        "shelves": [{"name": name, "things": things} for name, things in sorted(shelves.items())],
        "loose": loose,
        "pictures": pictures,
        "diary": diary_outline(root / DIARY_DIR),
        "showcase": [
            {**by_path[item["path"]], "note": item["note"], "since": item["since"]}
            for item in showcase
            if item["path"] in by_path
        ],
        "now": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def open_shown(room_dir, path) -> tuple[str, bytes]:
    """("text" or an image type, its bytes) for a thing in the showcase; NotShown otherwise."""
    room = Room(room_dir, room_dir)
    try:
        target = room.resolve(path)
        relative = room.show(target)
    except (ToolError, ValueError):
        raise NotShown from None
    on_show = {item["path"] for item in read_showcase(room_dir)}
    if relative not in on_show or relative.split("/")[0] == DIARY_DIR or not target.is_file():
        raise NotShown
    if target.is_symlink():
        raise NotShown
    limit = MAX_IMAGE_BYTES if kind_of(target) == "picture" else MAX_TEXT_BYTES
    if target.stat().st_size > limit:
        raise NotShown
    data = target.read_bytes()
    if kind := image_type(data[:16]):
        return kind, data
    if kind_of(target) != "text":
        raise NotShown
    return "text", data


def pieces(text: str) -> list[str]:
    return SENTENCE.findall(text)


def compare(old: str, new: str) -> list[dict]:
    """What stayed, what went and what is new between two versions, sentence by sentence;
    a sentence that was only reworded is compared character by character."""
    parts: list[dict] = []

    def add(op, text):
        if not text:
            return
        if parts and parts[-1]["op"] == op:
            parts[-1]["text"] += text
        else:
            parts.append({"op": op, "text": text})

    a, b = pieces(old), pieces(new)
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        before, after = "".join(a[i1:i2]), "".join(b[j1:j2])
        if tag == "equal":
            add("same", before)
            continue
        inner = SequenceMatcher(None, before, after, autojunk=False)
        # Reworded only when a real phrase survives; otherwise one went and another came.
        if inner.find_longest_match().size < 4:
            add("gone", before)
            add("new", after)
            continue
        gone = new_text = ""
        for step, p1, p2, q1, q2 in inner.get_opcodes():
            # A character or two in common between two changes is chance, not a kept phrase.
            if step == "equal" and (p2 - p1 >= 3 or not (gone or new_text)):
                add("gone", gone)
                add("new", new_text)
                gone = new_text = ""
                add("same", before[p1:p2])
            else:
                gone += before[p1:p2]
                new_text += after[q1:q2]
        add("gone", gone)
        add("new", new_text)
    return parts


def self_timeline(memory_path) -> list[dict]:
    """Every version of her self page, oldest first, each compared with the one before.

    A file in self-history/ is a page as it was until the moment in its name, so that
    moment is when the next version was written.
    """
    path = self_path(memory_path)
    history = sorted(
        (match.group(1), entry)
        for entry in (path.parent / HISTORY_DIR).glob("self-*.md")
        if (match := HISTORY_NAME.fullmatch(entry.name))
    )
    texts = [read_self(entry) for _, entry in history]
    written = [None]  # When the first one was written was never recorded.
    for name, _ in history:
        written.append(datetime.strptime(name, "%Y%m%d-%H%M%S").isoformat(timespec="seconds"))
    if current := read_self(path):
        texts.append(current)
    versions, previous = [], None
    for at, text in zip(written, texts, strict=False):
        parts = [{"op": "same", "text": text}] if previous is None else compare(previous, text)
        versions.append(
            {
                "at": at,
                "chars": len(text),
                "added": sum(len(p["text"]) for p in parts if p["op"] == "new"),
                "removed": sum(len(p["text"]) for p in parts if p["op"] == "gone"),
                "first": previous is None,
                "parts": parts,
            }
        )
        previous = text
    return versions


class RoomView:
    """Everything the page may ask for."""

    def __init__(self, room_dir, memory_path, monitor_dir, presence_path):
        self.room_dir = Path(room_dir)
        self.memory_path = Path(memory_path)
        self.watcher = Watcher(monitor_dir, presence_path)
        self.lock = threading.Lock()

    def now(self) -> dict:
        with self.lock:
            return self.watcher.poll()


PAGE = files("discord_dfy").joinpath("room.html")
SECURITY = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; img-src 'self'; connect-src 'self'; "
        "style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'"
    ),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "dfy-room"

    def log_message(self, *args):
        pass  # What he looks at in her room is not logged either.

    def reply(self, status, kind, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def data(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.reply(status, "application/json; charset=utf-8", body)

    def do_GET(self):
        # Only this computer's own browser, asking for this address by name: another site
        # cannot point a hostname here and read the answers.
        port = self.server.server_address[1]
        if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return self.data({"error": "只在本机看"}, 403)
        view: RoomView = self.server.view
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == "/":
                return self.reply(200, "text/html; charset=utf-8", PAGE.read_bytes())
            if url.path == "/api/room":
                return self.data(scan_room(view.room_dir))
            if url.path == "/api/self":
                return self.data(self_timeline(view.memory_path))
            if url.path == "/api/now":
                return self.data(view.now())
            if url.path == "/api/shown":
                kind, body = open_shown(view.room_dir, (query.get("path") or [""])[0])
                if kind == "text":
                    return self.data({"text": body.decode("utf-8", errors="replace")})
                return self.reply(200, f"image/{kind}", body)
        except NotShown:
            return self.data({"error": "这样东西她没摆出来"}, 404)
        except OSError:
            return self.data({"error": "读不到"}, 500)
        return self.data({"error": "没有这一页"}, 404)


def make_server(view: RoomView, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.view = view
    return server


def view_from_env() -> RoomView:
    """The same places the bot uses, from this directory's .env."""
    values = {**dotenv_values(Path.cwd() / ".env", interpolate=False), **os.environ}

    def place(name, default):
        return Path((values.get(name) or "").strip() or default)

    return RoomView(
        place("AGENT_ROOM_DIR", "data/room"),
        place("AGENT_MEMORY_PATH", "data/memory.md"),
        place("MONITOR_DIRECTORY", "data/monitor"),
        place("PERSONAL_DATABASE_PATH", "data/personal.sqlite3").parent / "presence.json",
    )


def main():
    parser = argparse.ArgumentParser(description="她的房间：本地只读页面，只在本机能打开")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="本机端口")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    try:
        server = make_server(view_from_env(), args.port)
    except OSError:
        parser.error(f"端口 {args.port} 用不了，换一个：--port 8766")
    address = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"她的房间：{address}（只读，只在本机；Ctrl+C 关掉，不影响她）")
    if not args.no_open:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
