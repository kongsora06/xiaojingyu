import importlib.util
import io
import json
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from discord_dfy.config import ConfigurationError, Settings
from discord_dfy.monitor import (
    EventTail,
    Renderer,
    database_snapshot,
    readonly_connection,
    snapshot_events,
)
from discord_dfy.monitor_protocol import (
    EventSpool,
    Publisher,
    StreamRedactor,
    validate_event,
)
from discord_dfy.observability import prepare_directory

LAB = Path(__file__).resolve().parents[2] / "dsh-lab"


class Sink:
    def __init__(self):
        self.events = []

    def offer(self, event):
        self.events.append(event)


class Bus:
    def __init__(self, sink=None):
        self.sink = sink or Sink()

    def publisher(self, source, **ids):
        return Publisher(self.sink, source, secrets=("private-key-123",), **ids)


def test_protocol_vendor_is_identical():
    from discord_dfy import monitor_protocol

    assert (
        Path(monitor_protocol.__file__).read_bytes()
        == (LAB / "scripts/monitor_protocol.py").read_bytes()
    )


@pytest.mark.parametrize("split", [1, 7, 30, 80])
def test_split_credentials_and_terminal_escape_sequences(split):
    key = "private-key-123"
    text = "中文原文 " + "x" * 100 + key + " sk-abcdefghijklmnopqrst "
    text += "\x1b]52;c;secret-clipboard\x1b\\\x1b[31m后文\x1b[0m"
    redactor = StreamRedactor((key,))
    output = "".join(redactor.feed(text[i : i + split]) for i in range(0, len(text), split))
    output += redactor.feed("", final=True)
    assert key not in output and "sk-abcdefghijklmnopqrst" not in output
    assert "secret-clipboard" not in output and "\x1b" not in output
    assert "中文原文" in output and "后文" in output


def test_hostile_long_token_has_bounded_buffer():
    redactor = StreamRedactor()
    output = redactor.feed("sk-" + "x" * 20000)
    assert len(redactor.pending) <= 8192 and "sk-" not in output


def test_event_validation_rejects_control_or_unknown_envelopes():
    sink = Sink()
    pub = Publisher(sink, "social", session_id="conversation", task_id="task")
    pub.event("task-status", status="running")
    event = sink.events[0]
    assert validate_event(event)
    assert not validate_event({**event, "command": "execute"})
    assert not validate_event({**event, "type": "cancel"})
    assert not validate_event({**event, "source": "shell"})
    assert not validate_event({**event, "source": []})
    assert not validate_event({**event, "data": {"status": []}})
    assert not validate_event({**event, "data": {"text": None}})


def test_queue_overflow_never_waits_for_consumer(tmp_path):
    gate = threading.Event()

    class Paused(EventSpool):
        def _write(self):
            gate.wait(2)
            super()._write()

    sink = Paused(tmp_path, capacity=2)
    pub = Publisher(sink, "social")
    start = time.monotonic()
    for _ in range(200):
        pub.event("task-status", status="running")
    assert sink.queue.qsize() == 2 and sink.dropped == 198
    assert time.monotonic() - start < 0.5
    gate.set()
    sink.close()
    assert sum(len(p.read_text().splitlines()) for p in tmp_path.glob("live-*.jsonl")) == 2


def test_spool_rotates_only_own_observation_files(tmp_path):
    preserved = tmp_path / "original.jsonl"
    preserved.write_text("old history")
    sink = EventSpool(tmp_path, file_bytes=300, retained=2)
    pub = Publisher(sink, "personal")
    for i in range(10):
        pub.event("task-status", status=str(i))
    sink.close()
    assert len(list(tmp_path.glob("live-*.jsonl"))) <= 2
    assert preserved.read_text() == "old history"


def test_disk_failure_and_closed_spool_are_observation_only(tmp_path, monkeypatch):
    def fail(*_, **__):
        raise OSError("private provider error")

    monkeypatch.setattr(Path, "touch", fail)
    sink = EventSpool(tmp_path)
    pub = Publisher(sink, "personal")
    pub.event("task-status", status="running")
    sink.close()
    assert sink.dropped == 1
    pub.event("task-status", status="completed")


def test_private_directory_refuses_unmanaged_files(tmp_path):
    (tmp_path / "keep.txt").write_text("preserve")
    with pytest.raises(OSError):
        prepare_directory(tmp_path)
    assert (tmp_path / "keep.txt").read_text() == "preserve"


def test_tail_follows_complete_lines_and_bounded_history(tmp_path):
    sink = Sink()
    pub = Publisher(sink, "personal", task_id="task")
    pub.event("task-status", status="running")
    path = tmp_path / ("live-" + "a" * 32 + ".jsonl")
    tail = EventTail([tmp_path], history=5)
    assert tail.poll() == []
    body = json.dumps(sink.events[0]).encode()
    path.write_bytes(body[:20])
    assert tail.poll() == []
    with path.open("ab") as stream:
        stream.write(body[20:] + b"\n")
    assert tail.poll() == sink.events
    assert tail.poll() == []
    path.write_bytes((body + b"\n") * 300)
    assert len(EventTail([tmp_path], history=5).poll()) == 5


def test_renderer_is_chinese_safe_and_rate_limited():
    stream = io.StringIO()
    now = [1.0]
    renderer = Renderer(stream, secrets=("private-key-123",), clock=lambda: now[0], verbose=True)
    renderer.write("中文 private-key-123 \x1b[31m内容\x1b[0m")
    for _ in range(20):
        renderer.event(
            {
                "v": 1,
                "source": "social",
                "type": "task-status",
                "timestamp": "2026-10-22T12:00:00+00:00",
                "session_id": None,
                "task_id": "task",
                "request_id": None,
                "data": {"status": "running"},
            }
        )
    assert renderer.refresh()
    assert not renderer.refresh()
    assert "中文" in stream.getvalue() and "内容" in stream.getvalue()
    assert "private-key-123" not in stream.getvalue() and "\x1b" not in stream.getvalue()
    assert stream.getvalue().count("执行中") == 20
    assert renderer.capacity_commits == 0


def test_sqlite_is_readonly_missing_files_not_created_and_schema_not_invented(tmp_path):
    path = tmp_path / "state.sqlite3"
    assert "状态" in database_snapshot(path, ("messages",))
    assert not path.exists()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE messages(message_id INTEGER,content TEXT)")
        db.execute("INSERT INTO messages VALUES(1,'hello')")
    with closing(readonly_connection(path)) as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("UPDATE messages SET content='changed'")
        with pytest.raises(sqlite3.OperationalError):
            db.execute("CREATE TABLE memory(x)")
    view = database_snapshot(path, ("messages", "personal_tasks", "memory"))
    assert view == {"messages": [{"message_id": 1, "content": "hello"}]}
    with sqlite3.connect(path) as db:
        db.execute("UPDATE messages SET content='new'")
    assert database_snapshot(path, ("messages",))["messages"][0]["content"] == "new"


def test_sqlite_preview_redacts_before_truncation(tmp_path):
    path = tmp_path / "state.sqlite3"
    key = "private-key-123"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE messages(content TEXT)")
        db.execute("INSERT INTO messages VALUES(?)", ("x" * 235 + key + "\x1b[31m",))
    view = database_snapshot(path, ("messages",), secrets=(key,))
    text = view["messages"][0]["content"]
    assert key[:5] not in text and "\x1b" not in text and len(text) <= 240


def test_locked_sqlite_reports_fixed_error_without_writes(tmp_path):
    path = tmp_path / "locked.sqlite3"
    with sqlite3.connect(path) as writer:
        writer.execute("CREATE TABLE messages(content TEXT)")
        writer.execute("BEGIN EXCLUSIVE")
        snapshot = database_snapshot(path, ("messages",))
        assert "数据库读取暂不可用" in snapshot["状态"]
        writer.rollback()


def test_sqlite_snapshot_envelope_has_actual_identifiers_and_no_mutation():
    snapshot = {
        "个人数据库": {
            "personal_tasks": [
                {"task_id": "task", "session_id": "session", "status": "running"},
            ]
        }
    }
    before = json.dumps(snapshot)
    events = snapshot_events(snapshot)
    assert len(events) == 1 and validate_event(events[0])
    assert events[0]["source"] == "sqlite" and events[0]["type"] == "database-snapshot"
    assert events[0]["session_id"] == "session" and events[0]["task_id"] == "task"
    assert json.dumps(snapshot) == before


def test_monitor_defaults_do_not_enable_personal_mode():
    values = {"DISCORD_BOT_TOKEN": "fake", "DEEPSEEK_API_KEY": "fake", "ALLOWED_CHANNEL_IDS": "100"}
    default = Settings.from_env(values)
    assert not default.monitor_enabled
    observed = Settings.from_env({**values, "MONITOR_ENABLED": "true"})
    assert observed.monitor_enabled
    assert not observed.personal_mode_enabled
    with pytest.raises(ConfigurationError, match="MONITOR_ENABLED"):
        Settings.from_env({**values, "MONITOR_ENABLED": "maybe"})


@pytest.mark.parametrize("show_db", [False, True])
def test_independent_cli_database_snapshots_are_opt_in(tmp_path, monkeypatch, show_db):
    from discord_dfy.conversation_store import SCHEMA as PERSONAL_SCHEMA
    from discord_dfy.storage import SCHEMA as SOCIAL_SCHEMA

    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(PERSONAL_SCHEMA + SOCIAL_SCHEMA)
        db.execute("INSERT INTO agent_conversations VALUES(200,1)")
        db.execute(
            "INSERT INTO agent_messages (channel_id,conversation,role,message,created_at) "
            "VALUES(200,1,'user','{}',1)"
        )
        db.execute("INSERT INTO users VALUES(20,'now','now')")
        db.execute(
            "INSERT INTO messages VALUES('message',1,10,100,20,?, 'now')",
            ("中文 secret-local-key",),
        )
        db.execute(
            "INSERT INTO social_decisions(created_at,channel_id,trigger,outcome) "
            "VALUES('now',100,'must','spoke')"
        )
    monitor = tmp_path / "monitor"
    monitor.mkdir()
    sink = EventSpool(monitor)
    pub = Publisher(sink, "personal", task_id="task", session_id="session")
    pub.text("reasoning-delta", "中文 original secret-local-key\x1b[31m", final=True)
    sink.close()
    (tmp_path / ".env").write_text(
        f'DATABASE_PATH="{path.as_posix()}"\nPERSONAL_DATABASE_PATH="{path.as_posix()}"\n'
        f'MONITOR_DIRECTORY="{monitor.as_posix()}"\nDEEPSEEK_API_KEY=secret-local-key\n',
        encoding="utf-8",
    )
    for key in ("DATABASE_PATH", "PERSONAL_DATABASE_PATH", "MONITOR_DIRECTORY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DSH_LAB_PATH", str(tmp_path / "absent-lab"))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "discord_dfy.monitor",
            "--once",
            "--history",
            "10",
            "--rows",
            "1",
            *(["--show-db"] if show_db else []),
        ],
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
    )
    output = result.stdout.decode("utf-8")
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert "推理原文" in output and "中文 original" in output
    assert ("数据库状态" in output) is show_db
    assert "secret-local-key" not in output and "\x1b" not in output
    for table in (
        "agent_messages",
        "agent_conversations",
        "messages",
        "social_decisions",
    ):
        assert (table in output) is show_db


def test_default_cli_does_not_read_sqlite(tmp_path, monkeypatch, capsys):
    from discord_dfy import monitor

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["dfy-monitor", "--once"])

    def unexpected_read(*args, **kwargs):
        pytest.fail("Default viewer must not query SQLite")

    monkeypatch.setattr(monitor, "database_snapshot", unexpected_read)
    monitor.main()
    assert "本地 Live Monitor" in capsys.readouterr().out


def load_lab(monkeypatch):
    monkeypatch.syspath_prepend(str(LAB / "scripts"))
    spec = importlib.util.spec_from_file_location("lab_monitor_tap", LAB / "scripts/monitor_tap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sse(event):
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()


def test_personal_sse_byte_fragments_preserve_order_and_never_change_bytes(monkeypatch):
    tap_module = load_lab(monkeypatch)
    sink = Sink()
    tap = tap_module.SSETap(Publisher(sink, "personal", task_id="task", session_id="session"))
    original = b"".join(
        [
            sse({"type": "message_start"}),
            sse(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "中文推理"},
                }
            ),
            sse(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "English result"},
                }
            ),
            sse({"type": "message_stop"}),
        ]
    )
    for byte in original:
        tap.feed(bytes([byte]))
    kinds = [e["type"] for e in sink.events]
    assert kinds.index("reasoning-delta") < kinds.index("text-delta")
    assert (
        "".join(e["data"].get("text", "") for e in sink.events if e["type"] == "reasoning-delta")
        == "中文推理"
    )
    assert all(validate_event(e) for e in sink.events)
    tap.feed(b"data: invalid\n\n")
    tap.feed(b"x" * 70000)
    assert len(tap.pending) <= 65536
    tap.close()
    assert sum(e["data"].get("status") == "completed" for e in sink.events) == 1


def test_personal_sse_failure_and_malformed_payloads_are_observation_only(monkeypatch):
    module = load_lab(monkeypatch)
    sink = Sink()
    tap = module.SSETap(Publisher(sink, "personal"))
    tap.feed(sse({"type": "content_block_delta", "delta": []}))
    tap.feed(
        sse(
            {
                "type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": "partial"},
            }
        )
    )
    tap.feed(sse({"type": "error", "error": {"message": "private provider string"}}))
    tap.close()
    assert sink.events[-1]["data"] == {"status": "failed"}
    assert "private provider string" not in str(sink.events)


def test_sdk_callback_disconnection_never_writes_stdout_or_raises(monkeypatch, capsys):
    module = load_lab(monkeypatch)
    sock = SimpleNamespace(
        setblocking=lambda _: None, sendto=lambda *_: (_ for _ in ()).throw(OSError())
    )
    monkeypatch.setattr(module.socket, "socket", lambda *_: sock)
    monkeypatch.setattr(module.socket, "AF_UNIX", 1, raising=False)
    callback, _ = module.sdk_callback(True)
    callback(
        SimpleNamespace(
            method="session.event",
            payload={
                "event": {
                    "type": "tool/call",
                    "data": {"name": "bash", "arguments": "echo synthetic"},
                }
            },
        )
    )
    assert capsys.readouterr().out == ""


def test_unavailable_sdk_observation_socket_disables_only_observation(monkeypatch):
    module = load_lab(monkeypatch)
    monkeypatch.setattr(module.socket, "AF_UNIX", 1, raising=False)

    def fail(*_):
        raise OSError("socket unavailable")

    monkeypatch.setattr(module.socket, "socket", fail)
    assert module.sdk_callback(True) == (None, None)
