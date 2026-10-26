"""Her one long-term memory: a Markdown file shared by the DM side and the group side.

The DM side adds lines with its remember tool and when it looks back over a chat; the
group side adds notes about the people and things in the group. Both read it. What the
owner says in DMs is in here too: the group side is told to keep those secrets.
"""

import asyncio
import logging
import re
from datetime import datetime

from openai import APIError

log = logging.getLogger(__name__)
# Never put more than this into a prompt, whatever happens to the file.
MEMORY_LIMIT_CHARS = 12000
GROUP_SECTION = "群里的人和事"
# Who she has become: written by her, more current than the persona's starting point.
SELF_SECTION = "我是什么样的"
WORK_SECTION = "做事备忘"
# Sections that looking back over a chat may add to.
MEMORY_SECTIONS = ("老爹", "我们之间", "我自己", SELF_SECTION, GROUP_SECTION)
# Past this size the file is tidied by the model (a backup is kept next to it).
MEMORY_TIDY_CHARS = 8000
MEMORY_TIDY_PROMPT = """下面是你（小鲸鱼）的记忆文件，里面有不少重复和啰嗦的地方。整理一遍：
- 保持原来的结构：开头的 "# memory" 和每个 "## " 小节标题都原样保留，条目放回原来的小节。
- 同一件事记了好几遍（哪怕说法不同）就合并成一条，保留最完整的细节和最早的日期。
- 明显过时或者被后来的事推翻的删掉；"做事备忘" 一节只合并重复，内容不要改。
- 别丢掉具体的事：谁说了什么、哪天、你当时的感受，这些是记忆的价值所在。
- "老爹" 一节里他吃了什么、几点上下班、几点睡这类流水账，合并成一两条稳定的习惯。
- 每条一行 "- "，日期留在条目末尾的括号里。
- 统一用第一人称：说到自己一律写"我"（把"小鲸鱼"改成"我"），说到老爹就写"老爹"。
只输出整理后的完整文件。"""
DATE_SUFFIX = re.compile(r"（(?:\d{4}-)?\d{1,2}-\d{1,2}）$")
_tidy_lock = asyncio.Lock()


def read_memory(path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def key(entry: str) -> str:
    """What makes two notes the same: the words, not the bullet, date or punctuation."""
    entry = DATE_SUFFIX.sub("", entry.strip().removeprefix("-").strip())
    return re.sub(r"[\s，。,.!！？?、~～]", "", entry).lower()


def first_person(note: str) -> str:
    """Her memory speaks of her as "我", never by name."""
    return note.replace("小鲸鱼", "我")


def add_memory_notes(path, notes: dict[str, list[str]]) -> int:
    """Append dated notes under their "## section" headings; returns how many were new."""
    text = read_memory(path)
    lines = text.splitlines() or ["# memory"]
    known = {key(line) for line in lines}
    stamp = datetime.now().strftime("%Y-%m-%d")
    added = 0
    for section, items in notes.items():
        new = []
        for item in items:
            item = first_person(DATE_SUFFIX.sub("", " ".join(str(item).split()))[:300].strip())
            if item and key(item) not in known:
                known.add(key(item))
                new.append(f"- {item}（{stamp}）")
        if not new:
            continue
        heading = f"## {section}"
        if heading in lines:
            start = lines.index(heading) + 1
            end = next(
                (i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines)
            )
            while end > start and not lines[end - 1].strip():
                end -= 1
            lines[end:end] = new
        elif f"## {WORK_SECTION}" in lines:
            # New sections go before the work notes, which stay last.
            at = lines.index(f"## {WORK_SECTION}")
            lines[at:at] = [heading, *new, ""]
        else:
            lines += ["", heading, *new]
        added += len(new)
    if added:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return added


def without_section(text: str, section: str) -> str:
    """The memory minus one "## section" (the group side has no use for work notes)."""
    kept, skipping = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            skipping = line == f"## {section}"
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip()


def section_text(text: str, section: str) -> str:
    lines, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line == f"## {section}"
            continue
        if inside:
            lines.append(line)
    return "\n".join(lines).strip()


def memory_block(memory: str) -> str:
    body = memory.strip() or "（还是空的）"
    if len(body) > MEMORY_LIMIT_CHARS:
        body = body[:MEMORY_LIMIT_CHARS] + "\n…（记忆太长，后面没有加载；请整理精简）"
    return f"\n\n<memory>\n{body}\n</memory>"


async def tidy_memory(client, model: str, path) -> bool:
    """Merge duplicates and drop stale notes; the previous file is kept as .bak."""
    if _tidy_lock.locked():
        return False
    async with _tidy_lock:
        before = await asyncio.to_thread(read_memory, path)
        if not before.strip():
            return False
        try:
            async with asyncio.timeout(300):
                response = await client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": MEMORY_TIDY_PROMPT},
                        {"role": "user", "content": before},
                    ],
                    max_tokens=8192,
                    extra_body={"thinking": {"type": "disabled"}},
                )
        except (TimeoutError, APIError) as error:
            log.warning("Memory tidy failed; error_type=%s", type(error).__name__)
            return False
        content = response.choices[0].message.content if response.choices else ""
        tidied = (content or "").strip()
        tidied = tidied.removeprefix("```markdown").removeprefix("```").removesuffix("```").strip()
        headings = [line for line in before.splitlines() if line.startswith("## ")]
        if (
            not tidied
            or not (len(before) * 0.3 < len(tidied) < len(before))
            or any(heading not in tidied.splitlines() for heading in headings)
        ):
            log.warning("Memory tidy result rejected")
            return False
        if await asyncio.to_thread(read_memory, path) != before:
            return False  # Edited meanwhile; try again another time.
        backup = path.with_name(path.name + ".bak")
        await asyncio.to_thread(backup.write_text, before, "utf-8")
        await asyncio.to_thread(path.write_text, tidied + "\n", "utf-8")
        log.info("Memory tidied: %d -> %d chars", len(before), len(tidied))
        return True
