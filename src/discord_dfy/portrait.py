"""Her self-portrait: "who I am", written and rewritten by her (data/self.md).

persona.md is only where she started. This page is her own account of herself:
temperament, likes, the way she talks, what she cares about. It is read right after the
starting persona and counts for more; it cannot change the rules that follow it. Every
rewrite keeps the previous version under self-history/, so nothing she was is lost.
"""

from datetime import datetime
from pathlib import Path

from .memory import first_person

SELF_LIMIT_CHARS = 2000
HISTORY_DIR = "self-history"


def self_path(memory_path) -> Path:
    """It lives next to her notebook."""
    return Path(memory_path).with_name("self.md")


def read_self(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def self_block(text: str) -> str:
    body = text.strip()[:SELF_LIMIT_CHARS] or "（还没写。）"
    return f"\n\n<self>\n（这是你自己写的你：比上面那页起点更新、更算数。）\n{body}\n</self>"


def write_self(path, text: str, now: datetime | None = None) -> str:
    """Replace the self-portrait; the old one is kept with a timestamp."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("写点什么再存")
    text = first_person(text.strip())
    if len(text) > SELF_LIMIT_CHARS:
        raise ValueError(f"太长了，{SELF_LIMIT_CHARS} 字以内")
    path = Path(path)
    old = read_self(path)
    if old == text:
        return "跟原来一样，没改"
    path.parent.mkdir(parents=True, exist_ok=True)
    if old:
        history = path.parent / HISTORY_DIR
        history.mkdir(exist_ok=True)
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        (history / f"self-{stamp}.md").write_text(old + "\n", encoding="utf-8")
    path.write_text(text + "\n", encoding="utf-8")
    return "写好了"
