"""Her self-portrait: "who I am", written and rewritten by her (data/self.md).

persona.md holds only what does not change: her name, that sora is her 主人, how
to talk, telling him the truth. Everything about who she is (temperament, likes, how she is
with 主人) lives on this one page, which she rewrites herself; until she has written it,
self_start.md stands in. It cannot change the rules that follow it. Every rewrite keeps the
previous version under self-history/, so nothing she was is lost.
"""

from datetime import datetime
from importlib.resources import files
from pathlib import Path

from .memory import first_person

SELF_LIMIT_CHARS = 4000
HISTORY_DIR = "self-history"
# Who she is until she writes her own page: temperament and how she is with 主人.
STARTING_SELF = files("discord_dfy").joinpath("self_start.md").read_text(encoding="utf-8").strip()


def self_path(memory_path) -> Path:
    """It lives next to her notebook."""
    return Path(memory_path).with_name("self.md")


def read_self(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def self_block(text: str) -> str:
    body = text.strip()[:SELF_LIMIT_CHARS] or f"（这是起点，你还没自己改过。）\n{STARTING_SELF}"
    return (
        "\n\n<self>\n（这是你自己写的你：脾气、性子、喜好、跟主人怎么相处，都以这页为准，"
        f"你自己改。）\n{body}\n</self>"
    )


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
