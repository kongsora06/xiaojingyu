"""He touched the little her on his desktop: the pet writes it down, she learns of it later.

The pet (dfy-pet) and the bot are two programs. The pet appends one line per touch to a
small file beside her other data; the DM side takes the file with his next message and tells
her in one line. Nothing is sent to her on the spot, and nothing she says comes back here.
"""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

FILE = "pet-touch.jsonl"
PAT, RUB = "pat", "rub"
# A touch he made longer ago than this is not brought up with his next message.
STALE = timedelta(hours=6)
# The bot may be off for days while he keeps poking her: the file stops growing here.
MAX_BYTES = 64_000
# Strokes closer together than this are one rub, however long he keeps at it.
RUB_GAP_SECONDS = 4.0


def touch_path(data_dir) -> Path:
    return Path(data_dir) / FILE


def record(path, kind: str, now: datetime | None = None) -> None:
    """Write one touch down. Never in the way: if the file cannot be written, it is lost."""
    path = Path(path)
    at = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > MAX_BYTES:
            return
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps({"at": at, "kind": kind}) + "\n")
    except OSError:
        pass


def take(path) -> list[tuple[str, datetime]]:
    """Every touch written down so far, oldest first; the file is emptied.

    The file is moved aside before it is read, so a touch the pet writes meanwhile starts a
    new one instead of being lost.
    """
    path = Path(path)
    taken = path.with_name(path.name + ".taken")
    try:
        os.replace(path, taken)
    except OSError:
        return []  # Nothing there, or the pet is writing this very moment: next time.
    try:
        text = taken.read_text(encoding="utf-8")
        taken.unlink()
    except OSError:
        return []
    touches = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
            kind, at = item["kind"], datetime.fromisoformat(item["at"])
        except (ValueError, TypeError, KeyError):
            continue
        if kind in (PAT, RUB) and at.tzinfo is not None:
            touches.append((kind, at))
    return sorted(touches, key=lambda touch: touch[1])


def touch_note(touches, now: datetime) -> str:
    """One line for her about what he did, or "" when there is nothing recent to tell."""
    fresh = [(kind, at) for kind, at in touches if now - at <= STALE]
    if not fresh:
        return ""
    rubs = sum(kind == RUB for kind, _ in fresh)
    pats = sum(kind == PAT for kind, _ in fresh)
    did = []
    if rubs:
        did.append("摸了摸你的头" if rubs == 1 else f"摸了 {rubs} 回你的头")
    if pats:
        did.append("拍了你一下" if pats == 1 else f"拍了你 {pats} 下")
    first, last = fresh[0][1].astimezone(), fresh[-1][1].astimezone()
    when = first.strftime("%m-%d %H:%M")
    if last - first >= timedelta(minutes=2):
        when += "–" + last.strftime("%H:%M" if last.date() == first.date() else "%m-%d %H:%M")
    return f"主人在桌面上{'、'.join(did)}（{when}，是他伸手碰了桌面上的你，不是他说的话）"


class Touches:
    """The pet's side: writes a pat for every click and one rub for a spell of rubbing."""

    def __init__(self, path):
        self.path = Path(path)
        self.last_stroke = None

    def pat(self) -> None:
        record(self.path, PAT)

    def rub(self, at: float | None = None) -> None:
        """One stroke over her head (at: seconds on any steady clock)."""
        at = time.monotonic() if at is None else at
        if self.last_stroke is None or at - self.last_stroke > RUB_GAP_SECONDS:
            record(self.path, RUB)
        self.last_stroke = at
