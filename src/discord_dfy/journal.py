"""Her day-by-day memory: recent days in detail, older weeks as one summary each.

The core memory file (data/memory.md) holds what stays true and is always loaded. Next
to it, data/memory/ holds the days:

  days/2026-10-29.md   one file per day, "- 14:35 [私聊] ★ ..." entries (★: might matter)
  days/old/            day files already folded into a week, kept forever
  weeks/2026-W43.md    one summary per past week
  weeks/index.md       one line per week, always loaded
  reports.md           what each consolidation kept, archived and trimmed
  dropped.md           core lines removed or rewritten when the core was trimmed

Every prompt gets the core, the last RECENT_DAYS days and the week index. Once a whole week
is older than that, consolidation looks back at it: what still matters goes into the core
almost word for word (hindsight decides, not the ★ alone), the rest becomes the week's
summary. Only this module and memory.py write these files.
"""

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from openai import APIError

from .memory import (
    MEMORY_SECTIONS,
    MEMORY_TIDY_CHARS,
    add_memory_notes,
    first_person,
    key,
    read_memory,
    tidy_memory,
)

log = logging.getLogger(__name__)
RECENT_DAYS = 14
# The recent days in a prompt; the oldest days go first when it is over.
JOURNAL_LIMIT_CHARS = 10000
PLACES = ("私聊", "群里")
# The remember tool's page for things that happened today.
JOURNAL_SECTION = "今天"
ENTRY = re.compile(r"^- (\d{2}:\d{2}) \[(私聊|群里)\] (★ )?(.+)$")
WEEKDAYS = "一二三四五六日"
# What a look-back over the archive may read in one request.
SEARCH_LIMIT_CHARS = 80000
_lock = asyncio.Lock()

CONSOLIDATE_PROMPT = """你是小鲸鱼。下面是你的长期记忆，
和你两个多星期前那一周每天记下的事（打 ★ 的是你当时觉得可能要紧的）。
现在回头看这一周：
- 到现在还算数、以后也想一直记着的，放进 keep，几乎照原话写，按小节分好。
  比如约定、老爹特意说给你听的话、你们之间要紧的时刻、关于谁的长久的事。
  当时打了 ★ 但现在看没那么要紧的，不放；没打 ★ 可后来又提起、其实挺要紧的，放。
  长期记忆里已经有的，不用再放。
- 别的写成这一周的回忆 summary：按日子说发生了什么，几段话，写具体（谁、什么事），流水账可以省。
- index 是一行字，让你以后一眼知道这周有过什么，比如“老爹考试周、Epric 生日、换了头像”。
说到自己写“我”，不写“小鲸鱼”。
只输出 JSON：
{"keep": {"老爹": [], "我们之间": [], "我自己": [], "我是什么样的": [], "群里的人和事": []},
 "summary": "", "index": ""}"""

SEARCH_PROMPT = """你是小鲸鱼。下面是你以前的记录：每周的回忆，和更早那些天每天记下的事。
从里面找出跟「{query}」有关的内容，带上日子，尽量照原话摘出来；可能有关的也算。
一条都没有就只回：没有"""


def journal_dir(memory_path) -> Path:
    """data/memory.md keeps its days in data/memory/."""
    return Path(memory_path).with_suffix("")


def day_path(memory_path, day: date) -> Path:
    return journal_dir(memory_path) / "days" / f"{day:%Y-%m-%d}.md"


def week_name(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def add_entries(memory_path, entries) -> int:
    """Add (when, place, text, important) entries to their day files; returns how many."""
    added = 0
    by_day: dict[date, list] = {}
    for when, place, text, important in entries:
        text = first_person(" ".join(str(text).split()))[:300].strip()
        if text and place in PLACES:
            by_day.setdefault(when.date(), []).append((when, place, text, important))
    for day, items in by_day.items():
        path = day_path(memory_path, day)
        lines = [line for line in read_memory(path).splitlines() if ENTRY.match(line)]
        known = {key(ENTRY.match(line).group(4)) for line in lines}
        new = 0
        for when, place, text, important in items:
            if key(text) in known:
                continue
            known.add(key(text))
            lines.append(f"- {when:%H:%M} [{place}] {'★ ' if important else ''}{text}")
            new += 1
        added += new
        if new:
            lines.sort(key=lambda line: line[2:7])  # By time; same minute keeps its order.
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {day:%Y-%m-%d}\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return added


def recent_text(memory_path, today: date, days: int = RECENT_DAYS) -> str:
    """The last days, oldest first, each under its own heading; trimmed from the oldest."""
    parts = []
    for back in range(days - 1, -1, -1):
        day = today - timedelta(days=back)
        lines = [
            line
            for line in read_memory(day_path(memory_path, day)).splitlines()
            if ENTRY.match(line)
        ]
        if lines:
            parts.append(f"## {day:%m-%d}（周{WEEKDAYS[day.weekday()]}）\n" + "\n".join(lines))
    while parts and sum(len(part) for part in parts) > JOURNAL_LIMIT_CHARS:
        parts.pop(0)
    return "\n\n".join(parts)


def index_text(memory_path) -> str:
    return read_memory(journal_dir(memory_path) / "weeks" / "index.md").strip()


def journal_block(memory_path, today: date) -> str:
    """The recent days and the week index, for a prompt."""
    recent = recent_text(memory_path, today) or "（最近还没记下什么）"
    block = (
        "\n\n<journal>\n（最近两个星期每天的事。[私聊] 是你和老爹私下的，[群里] 是群里的。"
        f"★ 是当时觉得可能要紧的。）\n{recent}\n</journal>"
    )
    if index := index_text(memory_path):
        block += (
            "\n\n<archive>\n（更早的每个星期只剩一行；想知道细节，就用 recall 翻。）\n"
            f"{index}\n</archive>"
        )
    return block


def due_weeks(memory_path, today: date) -> dict[str, list[Path]]:
    """Weeks whose every day is older than the recent window, with their day files."""
    cutoff = today - timedelta(days=RECENT_DAYS)
    weeks: dict[str, list[Path]] = {}
    folder = journal_dir(memory_path) / "days"
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        sunday = day + timedelta(days=6 - day.weekday())
        if sunday < cutoff:
            weeks.setdefault(week_name(day), []).append(path)
    return weeks


def _loads(text: str):
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def _ask(client, model, system, user, *, json_mode, max_tokens=4096) -> str | None:
    try:
        async with asyncio.timeout(300):
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=max_tokens,
                extra_body={"thinking": {"type": "disabled"}},
                **({"response_format": {"type": "json_object"}} if json_mode else {}),
            )
    except (TimeoutError, APIError) as error:
        log.warning("Journal request failed; error_type=%s", type(error).__name__)
        return None
    return (response.choices[0].message.content or "").strip() if response.choices else None


async def consolidate(client, model, memory_path, today: date | None = None) -> str | None:
    """Fold every due week into the archive; returns a short report, or None if nothing."""
    today = today or datetime.now().date()
    if _lock.locked():
        return None
    async with _lock:
        report = []
        for week, paths in (await asyncio.to_thread(due_weeks, memory_path, today)).items():
            line = await _fold_week(client, model, memory_path, week, paths)
            if line is None:
                break  # Try the rest another time, in order.
            report.append(line)
        if len(await asyncio.to_thread(read_memory, Path(memory_path))) > MEMORY_TIDY_CHARS:
            before = await asyncio.to_thread(read_memory, Path(memory_path))
            if await tidy_memory(client, model, Path(memory_path)):
                after = await asyncio.to_thread(read_memory, Path(memory_path))
                gone = await asyncio.to_thread(_note_dropped, memory_path, before, after, today)
                report.append(
                    f"长期记忆太长，整理了一遍（{len(before)} → {len(after)} 字，"
                    f"{gone} 条去掉或改写，原样留在 dropped.md）"
                )
        if not report:
            return None
        text = "\n".join(report)
        folder = journal_dir(memory_path)
        await asyncio.to_thread(_append, folder / "reports.md", f"## {today:%Y-%m-%d}\n{text}\n\n")
        log.info("Journal consolidated: %d steps", len(report))
        return text


async def _fold_week(client, model, memory_path, week, paths) -> str | None:
    days = "\n\n".join(path.read_text(encoding="utf-8").strip() for path in paths)
    core = await asyncio.to_thread(read_memory, Path(memory_path))
    content = await _ask(
        client,
        model,
        CONSOLIDATE_PROMPT,
        f"<memory>\n{core}\n</memory>\n\n<week>\n{days}\n</week>",
        json_mode=True,
    )
    data = _loads(content or "")
    summary = data.get("summary") if data else None
    if not isinstance(summary, str) or not summary.strip():
        log.warning("Journal consolidation returned no usable summary")
        return None
    keep = data.get("keep") if isinstance(data.get("keep"), dict) else {}
    notes = {
        section: [item for item in keep.get(section) or [] if isinstance(item, str)][:6]
        for section in MEMORY_SECTIONS
        if isinstance(keep.get(section), list)
    }
    first, last = date.fromisoformat(paths[0].stem), date.fromisoformat(paths[-1].stem)
    span = f"{first:%m-%d}~{last:%m-%d}"
    index = data.get("index") if isinstance(data.get("index"), str) else ""
    index = " ".join(index.split())[:80] or summary.strip().splitlines()[0][:40]

    def write():
        folder = journal_dir(memory_path)
        weeks = folder / "weeks"
        weeks.mkdir(parents=True, exist_ok=True)
        (weeks / f"{week}.md").write_text(
            f"# {week}（{span}）\n\n{first_person(summary.strip())}\n", encoding="utf-8"
        )
        _append(weeks / "index.md", f"- {span}：{first_person(index)}\n")
        kept = add_memory_notes(Path(memory_path), notes)
        old = folder / "days" / "old"
        old.mkdir(parents=True, exist_ok=True)
        for path in paths:
            path.replace(old / path.name)
        return kept

    kept = await asyncio.to_thread(write)
    kept_lines = [item for items in notes.values() for item in items]
    return f"{span} 收进了回忆（{index}）" + (
        f"；{kept} 条放进了长期记忆：" + "；".join(kept_lines)[:400] if kept else ""
    )


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(text)


def _note_dropped(memory_path, before: str, after: str, today: date) -> int:
    kept = {key(line) for line in after.splitlines()}
    gone = [line for line in before.splitlines() if line.startswith("- ") and key(line) not in kept]
    if gone:
        _append(
            journal_dir(memory_path) / "dropped.md",
            f"## {today:%Y-%m-%d}\n" + "\n".join(gone) + "\n\n",
        )
    return len(gone)


def archive_text(memory_path) -> str:
    """Week summaries and folded day files, newest kept when over the limit."""
    folder = journal_dir(memory_path)
    parts = [
        path.read_text(encoding="utf-8").strip()
        for pattern in ("weeks/20*.md", "days/old/*.md")
        for path in sorted(folder.glob(pattern))
    ]
    text = "\n\n".join(parts)
    return text[-SEARCH_LIMIT_CHARS:]


async def search_archive(client, model, memory_path, query: str) -> str | None:
    """What the older records say about something; None when there is nothing to search."""
    archive = await asyncio.to_thread(archive_text, memory_path)
    if not archive.strip():
        return None
    found = await _ask(
        client, model, SEARCH_PROMPT.format(query=query), archive, json_mode=False, max_tokens=2048
    )
    if not found or found.strip() in {"没有", "没有。"}:
        return ""
    return found.strip()


def day_text(memory_path, day: date) -> str:
    """One day's entries, whether still recent or already folded."""
    path = day_path(memory_path, day)
    old = path.parent / "old" / path.name
    return (read_memory(path) or read_memory(old)).strip()
