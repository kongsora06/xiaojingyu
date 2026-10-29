import json
from datetime import date, datetime
from types import SimpleNamespace

from discord_dfy import journal
from discord_dfy.journal import (
    add_entries,
    consolidate,
    day_path,
    day_text,
    due_weeks,
    index_text,
    journal_block,
    journal_dir,
    recent_text,
    search_archive,
)

TODAY = date(2026, 11, 20)  # A Friday.
# Two weeks after Sunday 11-08: the week of 11-02 is due.
LATER = date(2026, 11, 23)


class Client:
    """Scripted chat completions: each call pops the next reply and is recorded."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **request):
        self.calls.append(request)
        message = SimpleNamespace(content=self.replies.pop(0))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def at(day, hour=12, minute=0):
    return datetime(2026, 11, day, hour, minute)


def test_entries_land_on_their_day_in_time_order_once(tmp_path):
    memory = tmp_path / "memory.md"
    assert (
        add_entries(
            memory,
            [
                (at(19, 22, 5), "私聊", "小鲸鱼跟老爹说晚安", False),
                (at(19, 9), "群里", "Epric 叫我大肥鱼", True),
                (at(19, 23), "私聊", "小鲸鱼 跟老爹说晚安。", False),  # The same, said again.
                (at(20, 1), "别处", "不算", False),
            ],
        )
        == 2
    )
    page = day_path(memory, date(2026, 11, 19)).read_text(encoding="utf-8")
    assert page == (
        "# 2026-11-19\n- 09:00 [群里] ★ Epric 叫我大肥鱼\n- 22:05 [私聊] 我跟老爹说晚安\n"
    )
    assert not day_path(memory, date(2026, 11, 20)).exists()
    assert day_text(memory, date(2026, 11, 19)) == page.strip()


def test_prompt_gets_two_weeks_of_days_and_the_week_index(tmp_path, monkeypatch):
    memory = tmp_path / "memory.md"
    add_entries(memory, [(at(6), "私聊", "太早了，看不到", False)])
    add_entries(memory, [(at(7), "私聊", "两个星期前的今天", False)])
    add_entries(memory, [(at(20, 8), "群里", "今天的事", False)])
    recent = recent_text(memory, TODAY)
    assert "太早了" not in recent and recent.index("两个星期前") < recent.index("今天的事")
    assert "## 11-20（周五）\n- 08:00 [群里] 今天的事" in recent
    block = journal_block(memory, TODAY)
    assert block.startswith("\n\n<journal>\n") and "<archive>" not in block
    (journal_dir(memory) / "weeks").mkdir(parents=True)
    (journal_dir(memory) / "weeks" / "index.md").write_text("- 10-26~11-01：换了头像\n")
    assert "<archive>\n（更早的每个星期只剩一行" in journal_block(memory, TODAY)
    # Over the limit, the oldest days go first.
    monkeypatch.setattr(journal, "JOURNAL_LIMIT_CHARS", 40)
    assert recent_text(memory, TODAY) == "## 11-20（周五）\n- 08:00 [群里] 今天的事"
    assert "（最近还没记下什么）" in journal_block(tmp_path / "other.md", TODAY)


def test_a_week_is_due_once_its_last_day_is_past_the_window(tmp_path):
    memory = tmp_path / "memory.md"
    for day in (2, 8, 9):  # Mon 11-02 and Sun 11-08 (W45), Mon 11-09 (W46).
        add_entries(memory, [(at(day), "私聊", f"{day} 号的事", False)])
    assert due_weeks(memory, date(2026, 11, 22)) == {}
    weeks = due_weeks(memory, LATER)
    assert list(weeks) == ["2026-W45"] and [p.stem for p in weeks["2026-W45"]] == [
        "2026-11-02",
        "2026-11-08",
    ]
    assert list(due_weeks(memory, date(2026, 11, 30))) == ["2026-W45", "2026-W46"]


async def test_consolidation_keeps_what_still_matters_and_archives_the_rest(tmp_path):
    memory = tmp_path / "memory.md"
    memory.write_text("# memory\n\n## 老爹\n- 在店里打工（2026-10-25）\n", encoding="utf-8")
    add_entries(
        memory,
        [
            (at(2, 21), "私聊", "老爹说等我生日带我去看海", True),
            (at(3, 22), "群里", "Epric 发了猫图", False),
        ],
    )
    reply = {
        "keep": {"我们之间": ["老爹说等我生日带我去看海"], "老爹": ["在店里打工"]},
        "summary": "11-02 老爹说要带小鲸鱼去看海。11-03 群里发猫图。",
        "index": "老爹答应带我看海、群里猫图",
    }
    client = Client(json.dumps(reply, ensure_ascii=False))
    report = await consolidate(client, "m", memory, LATER)
    assert "11-02~11-03 收进了回忆（老爹答应带我看海、群里猫图）" in report
    assert "1 条放进了长期记忆" in report
    sent = client.calls[0]["messages"][1]["content"]
    assert "<memory>" in sent and "★ 老爹说等我生日带我去看海" in sent
    core = memory.read_text(encoding="utf-8")
    assert "## 我们之间\n- 老爹说等我生日带我去看海（" in core
    assert core.count("在店里打工") == 1
    folder = journal_dir(memory)
    week = (folder / "weeks" / "2026-W45.md").read_text(encoding="utf-8")
    assert week.startswith("# 2026-W45（11-02~11-03）") and "老爹说要带我去看海" in week
    assert index_text(memory) == "- 11-02~11-03：老爹答应带我看海、群里猫图"
    assert not list((folder / "days").glob("*.md"))
    assert sorted(p.name for p in (folder / "days" / "old").iterdir()) == [
        "2026-11-02.md",
        "2026-11-03.md",
    ]
    assert "老爹答应带我看海" in (folder / "reports.md").read_text(encoding="utf-8")
    # Nothing left to do: no request.
    assert await consolidate(client, "m", memory, LATER) is None and len(client.calls) == 1


async def test_a_bad_answer_leaves_the_week_for_next_time(tmp_path):
    memory = tmp_path / "memory.md"
    add_entries(memory, [(at(2), "私聊", "一件事", False)])
    assert await consolidate(Client("不是 JSON"), "m", memory, LATER) is None
    assert day_path(memory, date(2026, 11, 2)).exists()


async def test_an_overfull_core_is_trimmed_and_what_went_is_kept(tmp_path, monkeypatch):
    memory = tmp_path / "memory.md"
    lines = "".join(f"- 第{i}条很长很长的旧事（2026-10-2{i}）\n" for i in range(1, 7))
    memory.write_text(f"# memory\n\n## 我自己\n{lines}", encoding="utf-8")
    monkeypatch.setattr(journal, "MEMORY_TIDY_CHARS", 60)
    tidied = "# memory\n\n## 我自己\n- 第1条很长很长的旧事（2026-10-21）\n- 第2到6条合在一起"
    report = await consolidate(Client(tidied), "m", memory, TODAY)
    assert "长期记忆太长，整理了一遍" in report and "5 条去掉或改写" in report
    dropped = (journal_dir(memory) / "dropped.md").read_text(encoding="utf-8")
    assert dropped.startswith("## 2026-11-20\n- 第2条") and "第1条" not in dropped
    assert memory.with_name("memory.md.bak").exists()


async def test_searching_the_archive(tmp_path):
    memory = tmp_path / "memory.md"
    assert await search_archive(Client(), "m", memory, "看海") is None  # Nothing to search.
    weeks = journal_dir(memory) / "weeks"
    weeks.mkdir(parents=True)
    (weeks / "2026-W45.md").write_text("# 2026-W45\n\n老爹答应带我去看海。\n", encoding="utf-8")
    client = Client("11-02 老爹答应带我去看海", "没有")
    assert await search_archive(client, "m", memory, "看海") == "11-02 老爹答应带我去看海"
    assert "「看海」" in client.calls[0]["messages"][0]["content"]
    assert "老爹答应带我去看海" in client.calls[0]["messages"][1]["content"]
    assert await search_archive(client, "m", memory, "猫") == ""
