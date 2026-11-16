import json
from datetime import UTC, datetime, timedelta

from rich.text import Text

from discord_dfy.pet import kept_place
from discord_dfy.pet_monitor import COLOURS, FORE, INTRO, NO_EVENTS, Feed, Page, look, pieces


def event(kind, seconds_ago=0.0, request=None, **data):
    return {
        "v": 1,
        "source": "personal",
        "type": kind,
        "timestamp": (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat(),
        "session_id": "200",
        "task_id": "t1",
        "request_id": request,
        "data": data,
    }


def write(log, *events):
    with log.open("a", encoding="utf-8") as file:
        for item in events:
            file.write(json.dumps(item, ensure_ascii=False) + "\n")


def words(runs) -> str:
    return "".join(text for text, _ in runs)


def test_styled_text_becomes_runs_and_is_never_interpreted():
    text = Text("警告", style="red")
    assert pieces(text) == [("警告", "red")]
    frame = Text()
    frame.append("\n12:00:00 个人 · 会话 200\n", "bold blue")
    frame.append("[bold]不是标记[/bold] :smile:", "green")
    frame.append("\n")
    assert pieces(frame) == [
        ("\n12:00:00 个人 · 会话 200\n", "bold blue"),
        ("[bold]不是标记[/bold] :smile:", "green"),
        ("\n", ""),
    ]
    assert pieces(Text()) == []


def test_every_style_the_monitor_uses_has_a_look():
    assert look("bold blue") == (COLOURS["blue"], True)
    assert look("grey70") == (COLOURS["grey70"], False)
    assert look("") == (FORE, False) and look("blink") == (FORE, False)
    for style in ("green", "yellow", "red", "magenta", "blue"):
        assert look(style)[0] != FORE


def test_the_page_joins_what_is_printed_in_one_style():
    page = Page()
    page.print(Text("一", style="green"), end="")
    page.print(Text("二", style="green"), end="")
    page.print(Text("三", style="red"))
    assert page.take() == [("一二", "green"), ("三", "red"), ("\n", "")]
    assert page.take() == []


def test_the_window_shows_what_the_terminal_monitor_shows(tmp_path):
    log = tmp_path / ("live-" + "a" * 32 + ".jsonl")
    write(
        log,
        event("reasoning-delta", 9, "r1", text="他说早，"),
        event("reasoning-delta", 8, "r1", text="回一句。"),
        event("text-delta", 7, "r1", text="早呀 sk-abcdefghijklmnopqrstuvwxyz123456"),
        event("tool-call", 6, name="write_diary", text="写日记"),
        event("usage", 5, prompt_tokens=10),
        event("task-status", 4, status="completed"),
    )
    feed = Feed([tmp_path, tmp_path / "nowhere"], secrets=("my-bot-token",))
    first = feed.poll()
    assert first[0] == (INTRO, "blue") and NO_EVENTS not in words(first)
    shown = words(first)
    # Deltas of one answer are one unbroken passage under one heading.
    assert "推理原文 · 请求 r1\n他说早，回一句。\n" in shown and "最终回答 · 请求 r1\n早呀" in shown
    assert "工具调用  名称=write_diary\n写日记\n" in shown
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in shown  # Redacted as in the terminal.
    assert "实际用量" not in shown and "任务状态" not in shown  # Details stay hidden.
    styles = {style for text, style in first if "他说早" in text or "早呀" in text}
    assert styles == {"grey70", "green"}  # Thinking is dim, her words are green.
    assert feed.poll() == []  # Nothing new, nothing added; the intro comes once.
    write(log, event("tool-result", 0, name="write_diary", text="写好了 my-bot-token"))
    later = words(runs := feed.poll())
    assert "工具结果  名称=write_diary\n写好了 " in later and "my-bot-token" not in later
    assert INTRO not in later and {style for _, style in runs} <= {"green", ""}
    # "详细": the same events drawn again, now with what was hidden.
    detailed = words(feed.again(True))
    assert detailed.startswith(INTRO) and "实际用量  输入 Token=10" in detailed
    assert "任务状态  状态=已完成" in detailed and "他说早，回一句。" in detailed
    assert detailed.count("工具结果") == 1
    plain = words(feed.again(False))
    assert "实际用量" not in plain and "他说早，回一句。" in plain


def test_with_no_events_anywhere_it_says_how_to_get_some(tmp_path):
    feed = Feed([tmp_path / "monitor"])
    assert feed.poll() == [(INTRO, "blue"), (NO_EVENTS, "yellow")]
    assert feed.poll() == []
    (tmp_path / "monitor").mkdir()
    assert words(feed.again(False)) == INTRO  # The folder is there now: just nothing yet.


def test_a_spoiled_file_is_reported_and_the_rest_still_shows(tmp_path):
    log = tmp_path / ("live-" + "b" * 32 + ".jsonl")
    feed = Feed([tmp_path], history=0)
    feed.poll()
    log.write_text("not json\n", encoding="utf-8")
    write(log, event("tool-call", name="web_search", text="q"))
    shown = words(feed.poll() + feed.poll())
    assert "[观测丢失] 1 条记录/文件" in shown and "工具调用  名称=web_search" in shown


def test_the_window_comes_back_where_it_was_if_that_is_still_on_a_screen():
    screens = (0, 0, 2560, 1440)
    assert kept_place("540x660+1800+700", screens) == "540x660+1800+700"
    assert kept_place("540x660+-200+10", screens) == "540x660+-200+10"  # Partly off the left.
    assert kept_place("540x660+3000+10", screens) is None  # That display is gone.
    assert kept_place("540x660+100+1430", screens) is None  # Title bar below the screen.
    assert kept_place("540x660+-900+10", (-1920, 0, 2560, 1440)) == "540x660+-900+10"
    for broken in (None, 5, "", "540x660", "wide", "10x10+0+0"):
        assert kept_place(broken, screens) is None
