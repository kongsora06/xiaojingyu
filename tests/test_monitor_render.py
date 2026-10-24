"""Viewer-only regressions: preserve literal text across refreshes and interleaving."""

import io
import json

import pytest
from rich.console import Console

from discord_dfy.monitor import EventTail, Renderer


def event(
    kind="reasoning-delta",
    text="",
    *,
    task="task-aaaaaaaa",
    session="sess-aaaaaaaa",
    request="req-aaaaaaaa",
    source="personal",
    **data,
):
    return {
        "v": 1,
        "source": source,
        "type": kind,
        "timestamp": "2026-10-22T12:34:56+00:00",
        "task_id": task,
        "session_id": session,
        "request_id": request,
        "data": {**({"text": text} if text else {}), **data},
    }


@pytest.mark.parametrize(
    "parts",
    [
        ["D", "eep", "Seek", " ", "Har", "ness"],
        ["中", "文推", "理", "。", "\n", "  保留", "空格", "\n\n"],
        ["🙂", "e", "\u0301", " 👩", "\u200d", "💻", "\n", "𝄞"],
        [" leading", "  ", "spaces ", "\n", "", "next line", "\n"],
    ],
)
@pytest.mark.parametrize("kind", ["reasoning-delta", "text-delta"])
def test_fragments_are_literal_contiguous_text(parts, kind):
    out = io.StringIO()
    renderer = Renderer(out, no_color=True)
    for part in parts:
        renderer.event(event(kind, part))
        renderer.refresh(force=True)
    renderer.event(event("model-status", status="completed"))
    renderer.flush()
    assert "".join(parts) in out.getvalue()
    assert out.getvalue().count("请求 req-aaaa") == 1
    assert out.getvalue().count("个人 · 会话 sess-aaa") == 1
    assert "任务 task-aaa" not in out.getvalue()


def test_word_continues_after_real_partial_refresh_without_repeated_heading():
    out = io.StringIO()
    now = [1.0]
    renderer = Renderer(out, clock=lambda: now[0])
    prefix = "前文 " * 60
    renderer.event(event(text=prefix + "D"))
    renderer.refresh()
    assert "前文" in out.getvalue()  # Some text was already appended to the terminal.
    for part in ("eep", "Seek", " Harness\n", "  第二行🙂"):
        now[0] += 0.2
        renderer.event(event(text=part))
        renderer.refresh()
    renderer.flush()
    assert prefix + "DeepSeek Harness\n  第二行🙂" in out.getvalue()
    assert out.getvalue().count("推理原文") == 1


def test_default_view_hides_metadata_and_database_but_keeps_content_and_errors():
    out = io.StringIO()
    renderer = Renderer(out)
    renderer.event(event("task-status", status="running", name=""))
    renderer.event(event("model-status", status="started", model="synthetic-model"))
    renderer.event(event("usage", input_tokens=100, output_tokens=20))
    renderer.event(event(text="合成推理片段"))
    renderer.event(event("text-delta", "合成回答正文"))
    renderer.event(event("model-status", status="completed"))
    renderer.event(event("task-status", status="completed"))
    renderer.event(event("database-snapshot", '{"result":"重复的数据库正文"}', source="sqlite"))
    renderer.event(event("tool-call", name="bash", text="synthetic command"))
    renderer.event(event("tool-result", status="error", text="合成错误详情"))
    renderer.flush()
    output = out.getvalue()
    for text in ("合成推理片段", "合成回答正文", "工具调用", "合成错误详情"):
        assert text in output
    for text in ("数据库状态", "重复的数据库正文", "实际用量", "模型状态", "任务状态"):
        assert text not in output
    assert "名称=bash" in output


def test_details_can_be_enabled_explicitly():
    out = io.StringIO()
    renderer = Renderer(out, verbose=True, show_db=True)
    renderer.event(event("task-status", status="running", name=""))
    renderer.event(event("usage", input_tokens=100))
    renderer.event(event("database-snapshot", "合成数据库快照", source="sqlite"))
    renderer.flush()
    output = out.getvalue()
    assert "任务状态" in output and "实际用量" in output and "合成数据库快照" in output
    assert "名称=" not in output


@pytest.mark.parametrize("different", ["task", "request", "session", "source"])
def test_interleaved_identities_never_mix_words(different):
    out = io.StringIO()
    renderer = Renderer(out)
    other = {
        different: {
            "task": "task-bbbbbbbb",
            "request": "req-bbbbbbbb",
            "session": "sess-bbbbbbbb",
            "source": "social",
        }[different]
    }
    renderer.event(event(text="Deep"))
    renderer.event(event(text="Other", **other))
    renderer.refresh(force=True)
    renderer.event(event(text="Seek original\n"))
    renderer.event(event(text=" request 中文\n", **other))
    renderer.event(event("model-status", status="completed"))
    renderer.event(event("model-status", status="completed", **other))
    renderer.flush()
    output = out.getvalue()
    assert "DeepSeek original\n" in output and "Other request 中文\n" in output
    assert "DeepOther" not in output


def test_reasoning_and_final_answer_are_separate_blocks_even_with_interleaving():
    out = io.StringIO()
    renderer = Renderer(out)
    renderer.event(event(text="Think"))
    renderer.event(event(text="另一", request="req-bbbbbbbb"))
    renderer.event(event(text="ing original"))
    renderer.event(event("text-delta", "Final "))
    renderer.event(event("text-delta", "answer"))
    renderer.event(event(text="请求原文", request="req-bbbbbbbb"))
    renderer.event(event("model-status", request="req-bbbbbbbb", status="completed"))
    renderer.event(event("model-status", status="completed"))
    renderer.flush()
    output = out.getvalue()
    assert "Thinking original\n" in output and "Final answer\n" in output
    assert "另一请求原文\n" in output
    assert output.count("最终回答") == 1


@pytest.mark.parametrize("verbose", [False, True])
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "timed_out"])
@pytest.mark.parametrize("kind", ["model-status", "task-status"])
def test_all_terminal_states_flush_redaction_suffixes_before_status(status, kind, verbose):
    out = io.StringIO()
    renderer = Renderer(out, verbose=verbose)
    renderer.event(event(text="unfinished short tail"))
    renderer.refresh(force=True)
    renderer.event(
        event(kind, request=None if kind == "task-status" else "req-aaaaaaaa", status=status)
    )
    renderer.refresh(force=True)
    assert "unfinished short tail\n" in out.getvalue()
    label = "任务状态" if kind == "task-status" else "模型状态"
    if verbose or status != "completed":
        assert out.getvalue().index("unfinished short tail") < out.getvalue().index(label)
    else:
        assert label not in out.getvalue()
    assert not renderer.streams and not renderer.pending


def test_refresh_throttles_batches_without_dropping_large_fragment_burst():
    out = io.StringIO()
    now = [1.0]
    renderer = Renderer(out, clock=lambda: now[0])
    renderer.refresh()
    for _ in range(10000):
        renderer.event(event(text="词 x "))
        assert not renderer.refresh()
    assert renderer.buffered_chars <= renderer.max_buffer_chars
    renderer.flush()  # --once, Ctrl+C, and EOF flush without waiting for the next refresh.
    assert "词 x " * 10000 in out.getvalue()
    assert renderer.capacity_commits == 0 and renderer.buffered_chars == 0


def test_bounded_buffer_commits_without_losing_text_and_labels_continuations():
    out = io.StringIO()
    renderer = Renderer(out, max_buffer_chars=4096, max_items=4)
    for _ in range(6000):
        renderer.event(event(text="猫"))
        assert renderer.buffered_chars <= 4096
    renderer.flush()
    assert out.getvalue().count("猫") == 6000
    assert renderer.capacity_commits > 0 and "缓冲保护" in out.getvalue()
    assert renderer.lost_streams == 0


def test_capacity_bound_applies_to_many_interleaved_streams_and_metadata():
    out = io.StringIO()
    renderer = Renderer(out, max_items=4)
    for number in range(80):
        renderer.event(event(text=f"原文{number}。", request=f"request-{number}"))
        renderer.event(event("task-status", status="running", task=f"task-{number}"))
        assert len(renderer.pending) <= 4
    renderer.flush()
    assert len(renderer.streams) <= 4 and renderer.lost_streams > 0
    assert "观测丢失" in out.getvalue()


def test_capacity_commit_keeps_split_secret_and_control_filter_state():
    out = io.StringIO()
    renderer = Renderer(out, max_buffer_chars=512, secrets=("private-key-123",), verbose=True)
    renderer.event(event(text="a" * 430 + "private-key-"))
    renderer.event(event("task-status", status="running", model="x" * 160))
    assert renderer.capacity_commits > 0
    renderer.event(event(text="123 \x1b]52;c;clipboard"))
    renderer.event(event("task-status", status="running", model="x" * 160))
    renderer.event(event(text="-secret\x1b\\ 原文"))
    renderer.flush()
    output = out.getvalue()
    assert "private-key-" not in output and "clipboard" not in output
    assert "[REDACTED]" in output and "原文" in output


def test_sanitization_spans_fragments_and_markup_is_literal():
    out = io.StringIO()
    renderer = Renderer(out, secrets=("private-key-123",), no_color=True)
    text = "[red]literal[/red] private-key-123 \x1b]52;c;clipboard-secret\x1b\\"
    text += "\x1b[31m原文🙂\x1b[0m\n  spaces stay"
    for char in text:
        renderer.event(event(text=char))
        renderer.refresh(force=True)
    renderer.flush()
    output = out.getvalue()
    assert "[red]literal[/red] [REDACTED] 原文🙂\n  spaces stay" in output
    assert "private-key-123" not in output and "clipboard-secret" not in output
    assert "\x1b" not in output


def test_rich_colours_are_owned_by_viewer_not_model():
    out = io.StringIO()
    console = Console(
        file=out,
        force_terminal=True,
        color_system="256",
        no_color=False,
        width=120,
        markup=False,
        highlight=False,
        emoji=False,
    )
    renderer = Renderer(out, console=console, verbose=True)
    renderer.event(event("task-status", status="running"))
    renderer.event(event(text="[red]literal reasoning[/red]\x1b[35m"))
    renderer.event(event("text-delta", "literal answer"))
    renderer.event(event("model-status", status="completed"))
    renderer.event(event("tool-call", name="Shell", text="python -m pytest -q"))
    renderer.event(event("tool-result", text="5 passed", status="returned"))
    renderer.event(event("tool-result", text="synthetic failure", status="error"))
    renderer.flush()
    output = out.getvalue()
    for code in ("[34m", "[32m", "[33m", "[31m", "[38;5;"):
        assert "\x1b" + code in output
    assert "[red]literal reasoning[/red]" in output and "\x1b[35m" not in output
    assert "\x1b[2J" not in output and "\x1b[?1049" not in output  # No full-screen Live/TUI.


def test_reported_loss_breaks_stream_and_is_explicit():
    out = io.StringIO()
    renderer = Renderer(out)
    renderer.event(event(text="before"))
    renderer.event(event("dropped", count=10))
    renderer.event(event(text="after"))
    renderer.flush()
    assert "观测丢失" in out.getvalue() and "beforeafter" not in out.getvalue()


def test_tail_leaves_burst_on_disk_instead_of_silently_discarding_records(tmp_path):
    tail = EventTail([tmp_path])
    assert tail.poll() == []
    path = tmp_path / ("live-" + "a" * 32 + ".jsonl")
    records = [event(text=f"fragment-{i}") for i in range(750)]
    path.write_text("".join(json.dumps(e) + "\n" for e in records), encoding="utf-8")
    observed = []
    for _ in range(5):
        batch = tail.poll()
        assert len(batch) <= 200
        observed.extend(batch)
    assert observed == records and tail.take_lost() == 0


def test_tail_reports_unread_rotation_truncation_and_corrupt_frames(tmp_path):
    tail = EventTail([tmp_path])
    tail.poll()
    path = tmp_path / ("live-" + "b" * 32 + ".jsonl")
    path.write_text((json.dumps(event(text="x")) + "\n") * 500, encoding="utf-8")
    tail.poll()
    path.unlink()
    tail.poll()
    assert tail.take_lost() == 1
    path.write_text("bad-json\n", encoding="utf-8")
    tail.poll()
    assert tail.take_lost() == 1
    path.write_text("{}\n", encoding="utf-8")
    tail.poll()
    assert tail.take_lost() >= 1


def test_tail_merges_files_before_terminal_status_even_when_one_file_is_backlogged(tmp_path):
    tail = EventTail([tmp_path])
    tail.poll()
    records = []
    for i in range(750):
        entry = event(text="拼")
        entry["timestamp"] = f"2026-10-22T12:00:00.{i:06d}+00:00"
        records.append(entry)
    terminal = event("model-status", status="completed")
    terminal["timestamp"] = "2026-10-22T12:00:01+00:00"
    (tmp_path / ("live-" + "a" * 32 + ".jsonl")).write_text(
        "".join(json.dumps(e) + "\n" for e in records), encoding="utf-8"
    )
    (tmp_path / ("live-" + "b" * 32 + ".jsonl")).write_text(
        json.dumps(terminal) + "\n", encoding="utf-8"
    )
    out = io.StringIO()
    renderer = Renderer(out)
    observed = []
    for _ in range(5):
        batch = tail.poll()
        observed.extend(batch)
        for entry in batch:
            renderer.event(entry)
        renderer.refresh(force=True)
    assert observed == [*records, terminal]
    assert "拼" * 750 in out.getvalue() and out.getvalue().count("推理原文") == 1


def test_history_selects_latest_events_across_directories_with_bounded_window(tmp_path):
    newer, older = tmp_path / "new", tmp_path / "old"
    newer.mkdir()
    older.mkdir()
    for directory, timestamp in (
        (newer, "2026-10-22T13:00:00+00:00"),
        (older, "2026-10-22T12:00:00+00:00"),
    ):
        entry = event(text=directory.name)
        entry["timestamp"] = timestamp
        (directory / ("live-" + "a" * 32 + ".jsonl")).write_text(
            (json.dumps(entry) + "\n") * 250, encoding="utf-8"
        )
    assert all(e["data"]["text"] == "new" for e in EventTail([newer, older], history=5).poll())
