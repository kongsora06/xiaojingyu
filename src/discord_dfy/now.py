"""What she is doing right now, read from the Live Monitor's event files.

Only a label comes out ("在写日记", "在上网搜东西"): never her words, her thinking, or what a
tool was given. The room page and the desktop widget both use it; both only read.
Without MONITOR_ENABLED there are no events, and all that is known is her status line.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from .monitor import EventTail

# Tool name -> (while she is at it, once it is done). None: looking around leaves no trace.
DOING = {
    "write_diary": ("在写日记", "写了日记"),
    "read_diary": ("在翻日记", None),
    "room_write": ("在房间里写东西", "在房间里写了东西"),
    "room_read": ("在房间里翻东西", None),
    "room_list": ("在房间里转悠", None),
    "room_show": ("在摆橱窗", "动了动橱窗"),
    "save_picture": ("在收图", "收了一张图"),
    "view_image": ("在看图", None),
    "web_search": ("在上网搜东西", "上网搜了东西"),
    "web_fetch": ("在看网页", "看了一个网页"),
    "remember": ("在小本子上记东西", "在小本子上记了一笔"),
    "recall": ("在翻旧事", None),
    "rewrite_self": ("在重写自己那一页", "重写了自己那一页"),
    "group_post": ("在群里替你传话", "去群里说了话"),
    "group_read": ("在看群里聊什么", None),
    "update_profile": ("在改自己的资料", "改了自己的资料"),
    "send_file": ("在给你发东西", "给你发了样东西"),
}
OWN_TIME = "自己待着"
# Nothing new for this long means whatever she was doing has ended (a slow model call or
# a web page can stay silent for a while, so not too short).
QUIET_SECONDS = 90
HISTORY_EVENTS = 200


def moment(event) -> datetime:
    try:
        at = datetime.fromisoformat(event["timestamp"])
    except ValueError:
        return datetime.now(UTC)
    return at if at.tzinfo else at.replace(tzinfo=UTC)


class Now:
    """Fold monitor events into one line about what she is doing."""

    def __init__(self):
        self.doing = None
        self.where = ""
        self.at = None
        self.did = None
        self.did_at = None

    def feed(self, event) -> None:
        source, kind, data = event["source"], event["type"], event["data"]
        if source == "sqlite" or kind in {"usage", "dropped", "database-snapshot"}:
            return
        at = moment(event)
        if source == "social":
            where = "群里"
        elif event["session_id"] == OWN_TIME:
            where = OWN_TIME
        else:
            where = "和你聊天"
        doing = self.doing
        if kind == "reasoning-delta":
            doing = "在想"
        elif kind == "text-delta":
            doing = "在说话"
        elif kind == "tool-call":
            doing, did = DOING.get(data.get("name"), ("在忙", None))
            if did:
                self.did, self.did_at = did, at
        elif kind == "tool-result":
            doing = "在想"
        elif kind == "decision":
            doing = None
        elif kind == "model-status":
            status = data.get("status")
            doing = "在想" if status == "started" else doing if status == "completed" else None
        elif kind == "task-status":
            doing = "在看你说的话" if data.get("status") == "running" else None
        self.doing, self.where, self.at = doing, where if doing else "", at

    def snapshot(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        live = self.doing is not None and (now - self.at).total_seconds() < QUIET_SECONDS
        return {
            "doing": self.doing if live else None,
            "where": self.where if live else "",
            "since": self.at.isoformat() if self.at else None,
            "did": self.did,
            "did_at": self.did_at.isoformat() if self.did_at else None,
        }


def read_presence(path) -> dict:
    """Her status line and online state, as she last set them."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"text": "", "state": "online"}
    if not isinstance(data, dict):
        return {"text": "", "state": "online"}
    return {"text": str(data.get("text") or ""), "state": str(data.get("state") or "online")}


class Watcher:
    """Follow the monitor directory; poll() gives the current picture."""

    def __init__(self, monitor_dir, presence_path):
        self.monitor_dir = Path(monitor_dir)
        self.presence_path = Path(presence_path)
        self.tail = EventTail([self.monitor_dir], history=HISTORY_EVENTS)
        self.now = Now()

    def poll(self) -> dict:
        for _ in range(20):  # At most 200 events a poll; catch up after a busy spell.
            events = self.tail.poll()
            for event in events:
                self.now.feed(event)
            if len(events) < 200:
                break
        self.tail.take_lost()
        return {
            **self.now.snapshot(),
            "status": read_presence(self.presence_path),
            "watching": self.monitor_dir.is_dir() and self.now.at is not None,
        }


def ago(then: str | None, now: datetime | None = None) -> str:
    """ "刚刚", "12 分钟前", "3 小时前", "2 天前"; empty when unknown."""
    if not then:
        return ""
    try:
        seconds = ((now or datetime.now(UTC)) - datetime.fromisoformat(then)).total_seconds()
    except (ValueError, TypeError):
        return ""
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"
