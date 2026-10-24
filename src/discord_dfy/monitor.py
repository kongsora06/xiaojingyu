"""Independent Chinese terminal viewer. Read-only files/SQLite; no agent control channel."""

import argparse
import heapq
import json
import os
import sqlite3
import sys
import time
from collections import deque
from contextlib import ExitStack, closing
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values
from rich.console import Console
from rich.text import Text

from .monitor_protocol import (
    FILE_NAME,
    MAX_EVENT_BYTES,
    Publisher,
    StreamRedactor,
    safe_text,
    validate_event,
)

TABLES = {
    "agent_messages": ("id", "channel_id", "conversation", "role", "message", "created_at"),
    "agent_conversations": ("channel_id", "current"),
    "messages": (
        "message_id",
        "source",
        "guild_id",
        "channel_id",
        "user_id",
        "content",
        "created_at",
    ),
    "social_decisions": (
        "id",
        "created_at",
        "channel_id",
        "trigger",
        "outcome",
        "sent",
        "reaction",
        "prompt_tokens",
        "cached_tokens",
        "completion_tokens",
    ),
}
LABELS = {"social": "社交", "personal": "个人", "sqlite": "数据库"}
KINDS = {
    "reasoning-delta": "推理原文",
    "text-delta": "最终回答",
    "tool-call": "工具调用",
    "tool-result": "工具结果",
    "task-status": "任务状态",
    "model-status": "模型状态",
    "usage": "实际用量",
    "decision": "决策",
    "dropped": "观测丢弃",
    "database-snapshot": "数据库状态",
}
STATES = {
    "starting": "准备中",
    "started": "已开始",
    "running": "执行中",
    "idle": "空闲",
    "completed": "已完成",
    "ended": "响应结束",
    "failed": "失败",
    "cancelled": "已取消",
    "timed_out": "超时",
    "invalid_output": "输出未通过校验",
    "returned": "已返回",
    "error": "错误",
    "queued": "等待中",
    "cancelling": "正在取消",
    "interrupted": "已中断",
    "executed": "已执行",
    "blocked": "已阻止",
    "delivery_error": "发送失败",
    "dry_run": "演练",
    "silence": "保持安静",
    "filtered": "已过滤",
    "pending": "待执行",
}
FIELDS = {
    "model": "模型",
    "status": "状态",
    "name": "名称",
    "code": "代码",
    "action": "决策",
    "reason": "原因",
    "executed": "已执行",
    "table": "表",
    "database": "数据库",
    "row": "行",
    "prompt_tokens": "输入 Token",
    "input_tokens": "输入 Token",
    "completion_tokens": "输出 Token",
    "output_tokens": "输出 Token",
    "total_tokens": "合计 Token",
    "reasoning_tokens": "推理 Token",
    "cache_read_input_tokens": "缓存读取 Token",
    "cache_creation_input_tokens": "缓存写入 Token",
}


def readonly_connection(path):
    # mode=ro never creates a missing database; do not use immutable=1 for a live WAL database.
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.05)
    db.execute("PRAGMA query_only=ON")
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    deadline = time.monotonic() + 0.1
    return db


def database_snapshot(path, tables, *, rows=3, secrets=()):
    snapshot = {}
    if not Path(path).is_file():
        return {"状态": "数据库尚不存在"}
    try:
        with closing(readonly_connection(path)) as db:
            existing = {
                r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table in tables:
                if table not in TABLES or table not in existing:
                    continue
                columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
                fields = [name for name in TABLES[table] if name in columns]
                selection = ",".join(
                    f'CASE WHEN typeof("{name}")=\'text\' THEN substr("{name}",1,4096) '
                    f'ELSE "{name}" END'
                    for name in fields
                )
                if not fields:
                    continue
                data = db.execute(
                    f'SELECT {selection} FROM "{table}" ORDER BY rowid DESC LIMIT ?',
                    (min(10, max(1, rows)),),
                ).fetchall()
                snapshot[table] = [
                    dict(
                        zip(
                            fields,
                            [safe_text(v, secrets)[:240] if isinstance(v, str) else v for v in row],
                            strict=True,
                        )
                    )
                    for row in data
                ]
    except sqlite3.Error:
        return {"状态": "数据库读取暂不可用（只读；未重试写入）"}
    return snapshot


def snapshot_events(snapshot, *, secrets=()):
    """SQLite snapshots use the same passive envelope, without writing another event log."""
    events = []

    class Sink:
        def offer(self, event):
            events.append(event)

    for database, tables in snapshot.items():
        for table, rows in tables.items():
            pub = Publisher(Sink(), "sqlite", secrets=secrets)
            if not isinstance(rows, list) or not rows:
                pub.event(
                    "database-snapshot",
                    database=database,
                    table=table,
                    text=rows if isinstance(rows, str) else "暂无记录",
                )
            else:
                for number, row in enumerate(rows, 1):
                    pub = Publisher(
                        Sink(),
                        "sqlite",
                        secrets=secrets,
                        session_id=row.get("session_id", row.get("conversation_id")),
                        task_id=row.get("task_id", row.get("decision_id")),
                    )
                    pub.event(
                        "database-snapshot",
                        database=database,
                        table=table,
                        row=number,
                        text=json.dumps(row, ensure_ascii=False),
                    )
    return events


class EventTail:
    def __init__(self, directories, *, history=0):
        self.directories = directories
        self.offsets = {}
        self.history = history
        self.initialized = False
        self.sizes = {}
        self.lost = 0

    def _next(self, path, stream):
        # A valid candidate is only committed after it is selected for this poll.
        # Malformed records are skipped once, partial appends are retried.
        for _ in range(200):
            start = stream.tell()
            line = stream.readline(MAX_EVENT_BYTES + 1)
            if not line:
                return None
            if not line.endswith(b"\n"):
                if len(line) <= MAX_EVENT_BYTES:
                    stream.seek(start)
                else:
                    self.lost += 1
                    stream.seek(0, 2)
                    self.offsets[path] = stream.tell()
                return None
            try:
                event = json.loads(line) if len(line) <= MAX_EVENT_BYTES else None
                if validate_event(event):
                    return event
            except (ValueError, UnicodeError):
                pass
            self.lost += 1
            self.offsets[path] = stream.tell()
        return None

    def poll(self):
        paths = []
        for directory in self.directories:
            if not directory.is_dir() or directory.is_symlink():
                continue
            try:
                paths.extend(
                    sorted(
                        (
                            p
                            for p in directory.iterdir()
                            if FILE_NAME.fullmatch(p.name) and not p.is_symlink()
                        ),
                        key=lambda p: p.stat().st_mtime_ns,
                    )[-8:]
                )
            except OSError:
                continue
        live = set(paths)
        for path, offset in self.offsets.items():
            if path not in live and offset < self.sizes.get(path, offset):
                self.lost += 1
        self.offsets = {p: v for p, v in self.offsets.items() if p in live}
        self.sizes = {p: v for p, v in self.sizes.items() if p in live}
        history = []
        output, candidates, serial = [], [], 0
        with ExitStack() as stack:
            streams = {}
            for path in paths:
                try:
                    stream = stack.enter_context(path.open("rb"))
                    streams[path] = stream
                    self.sizes[path] = path.stat().st_size
                    if not self.initialized:
                        if not self.history:
                            stream.seek(0, 2)
                            self.offsets[path] = stream.tell()
                        else:
                            for _ in range(8192):
                                event = self._next(path, stream)
                                if event is None:
                                    break
                                heapq.heappush(history, (event["timestamp"], serial, event))
                                serial += 1
                                if len(history) > 200:
                                    heapq.heappop(history)
                                self.offsets[path] = stream.tell()
                    else:
                        previous = self.offsets.get(path, 0)
                        if previous > self.sizes[path]:
                            self.lost += 1
                            previous = 0
                        stream.seek(previous)
                        self.offsets[path] = previous
                        event = self._next(path, stream)
                        if event is not None:
                            heapq.heappush(
                                candidates, (event["timestamp"], serial, path, event, stream.tell())
                            )
                            serial += 1
                except OSError:
                    continue
            # Merge one lookahead per file. A terminal status in a short file cannot
            # overtake earlier text left unread in a busy file. Never advance an
            # unselected candidate's saved offset or discard overflow deltas.
            while candidates and len(output) < 200:
                _, _, path, event, end = heapq.heappop(candidates)
                output.append(event)
                self.offsets[path] = end
                stream = streams[path]
                try:
                    following = self._next(path, stream)
                    if following is not None:
                        heapq.heappush(
                            candidates,
                            (following["timestamp"], serial, path, following, stream.tell()),
                        )
                        serial += 1
                except OSError:
                    continue
        if not self.initialized and self.history:
            output = [entry[2] for entry in sorted(history)[-self.history :]]
        self.initialized = True
        return output

    def take_lost(self):
        lost, self.lost = self.lost, 0
        return lost


TEXT_TYPES = {"reasoning-delta", "text-delta"}
TERMINAL = {
    "completed",
    "ended",
    "failed",
    "cancelled",
    "timed_out",
    "interrupted",
    "invalid_output",
}
ERRORS = {"failed", "error", "timed_out", "interrupted", "invalid_output", "delivery_error"}


@dataclass
class StreamBlock:
    event: dict
    redactor: StreamRedactor
    parts: list[str] = field(default_factory=list)
    size: int = 0
    closed: bool = False
    started: bool = False
    newline: bool = True
    queued: bool = False

    def append(self, text):
        if text:
            self.parts.append(text)
            self.size += len(text)


class Renderer:
    """Append-only Rich log. Interleaved open blocks wait rather than split a word.

    All state/output belongs to the independent viewer, never the agent/SDK path.
    Refresh throttles batch writes, not individual deltas. Capacity pressure commits
    buffered blocks early, preserving text and explicitly labelling continuation.
    """

    def __init__(
        self,
        stream,
        *,
        secrets=(),
        refresh_rate=5,
        no_color=False,
        verbose=False,
        show_db=False,
        max_buffer_chars=262144,
        max_items=128,
        clock=time.monotonic,
        console=None,
    ):
        if not 1 <= refresh_rate <= 20 or max_buffer_chars < 256 or max_items < 2:
            raise ValueError("Invalid renderer limits")
        self.secrets = secrets
        self.verbose, self.show_db = verbose, show_db
        self.console = console or Console(
            file=stream,
            color_system=None if no_color else "auto",
            no_color=True if no_color else None,
            markup=False,
            highlight=False,
            emoji=False,
        )
        self.clock, self.period, self.next_refresh = clock, 1 / refresh_rate, 0
        self.max_buffer_chars, self.max_items = max_buffer_chars, max_items
        self.pending = deque()
        self.streams = {}
        self.context = None
        self.capacity_commits = 0
        self.lost_streams = 0

    @property
    def buffered_chars(self):
        return sum(
            item.size
            if isinstance(item, StreamBlock)
            else len(json.dumps(item, ensure_ascii=False))
            for item in self.pending
        ) + sum(len(block.redactor.pending) for block in self.streams.values())

    @staticmethod
    def identity(event):
        return tuple(event[key] for key in ("source", "session_id", "task_id", "request_id"))

    def write(self, text, style="blue"):
        # Text() never parses model text as markup, emoji syntax, or ANSI.
        self.console.print(Text(safe_text(text, self.secrets), style=style), soft_wrap=True)

    def _seal(self, key):
        block = self.streams.pop(key)
        block.append(block.redactor.feed("", final=True))
        block.closed = True
        if not block.queued and block.parts:
            self.pending.append(block)
            block.queued = True

    def _capacity_commit(self, *, announce=True):
        # Never finalize a redactor just to free display capacity. A credential or
        # terminal sequence may be completed by a future fragment of this request.
        opened = list(self.streams.values())
        for block in opened:
            block.closed = True
        self.refresh(force=True)
        for block in opened:
            block.closed = block.started = False
            block.newline = True
        if announce:
            self.capacity_commits += 1
            self.write(
                "[缓冲保护] 已提前提交安全正文；后续为同标识续段，脱敏尾部仍保留。", "yellow"
            )

    def _evict_stream(self):
        key = next(iter(self.streams))
        block = self.streams.pop(key)
        self.lost_streams += 1
        self.write(
            f"[观测丢失] 并发缓冲超限，省略一个请求的 {len(block.redactor.pending)} 字符尾部；"
            "后续文字不保证完整。",
            "red",
        )

    def _room(self, incoming):
        if self.pending and (
            len(self.pending) >= self.max_items
            or self.buffered_chars + incoming > self.max_buffer_chars
        ):
            self._capacity_commit()
        while self.streams and self.buffered_chars + incoming > self.max_buffer_chars:
            self._evict_stream()

    def event(self, event):
        if not validate_event(event):
            self.warning("跳过不符合协议的事件")
            return
        identity, kind = self.identity(event), event["type"]
        if kind in TEXT_TYPES:
            # A content-type boundary seals only this request, never another task/request.
            for key in list(self.streams):
                if key[:-1] == identity and key[-1] != kind:
                    self._seal(key)
            text = event["data"].get("text", "")
            for offset in range(0, len(text), min(2048, self.max_buffer_chars // 2)):
                fragment = text[offset : offset + min(2048, self.max_buffer_chars // 2)]
                self._room(len(fragment))
                key = (*identity, kind)
                if key not in self.streams:
                    if len(self.streams) >= self.max_items:
                        self._capacity_commit()
                        self._evict_stream()
                    block = StreamBlock({**event, "data": {}}, StreamRedactor(self.secrets))
                    self.streams[key] = block
                block = self.streams[key]
                if not block.queued:
                    self.pending.append(block)
                    block.queued = True
                block.append(block.redactor.feed(fragment))
        else:
            if kind in {"model-status", "task-status"} and event["data"].get("status") in TERMINAL:
                for key in list(self.streams):
                    if key[:3] == identity[:3] and (
                        kind == "task-status" or identity[3] is None or key[3] == identity[3]
                    ):
                        self._seal(key)
            if kind == "dropped":
                self.warning("上游报告观测丢失；文字可能不完整")
            # Hidden terminal events must still seal streams and flush redaction tails.
            if kind == "database-snapshot" and not self.show_db:
                return
            if (
                not self.verbose
                and kind in {"model-status", "task-status", "usage", "decision"}
                and event["data"].get("status") not in ERRORS | {"cancelled"}
            ):
                return
            self._room(len(json.dumps(event, ensure_ascii=False)))
            self.pending.append(event)

    def _heading(self, frame, event):
        context = self.identity(event)[:3]
        if context != self.context:
            self.context = context

            def short(value):
                return safe_text(value, self.secrets)[:8] if value else "-"

            label = LABELS[event["source"]]
            if self.verbose:
                label += f"任务 {short(event['task_id'])}"
            frame.append(
                f"\n{event['timestamp'][11:19]} {label} · 会话 {short(event['session_id'])}\n",
                "bold blue",
            )

    def _metadata(self, frame, event):
        self._heading(frame, event)
        data = {key: value for key, value in event["data"].items() if value != ""}
        status = data.get("status")
        style = (
            "red"
            if status in ERRORS or event["type"] == "dropped"
            else "yellow"
            if event["type"] == "tool-call"
            else "green"
            if event["type"] == "tool-result"
            or (status == "executed" and event["type"] == "decision")
            else "blue"
        )
        content = data.pop("text", "")
        if status is not None:
            data["status"] = STATES.get(status, status)
        details = " · ".join(f"{FIELDS.get(k, k)}={v}" for k, v in data.items())
        frame.append(f"{KINDS[event['type']]}  {safe_text(details, self.secrets)}\n", style)
        if content:
            frame.append(safe_text(content, self.secrets), style)
            if not content.endswith("\n"):
                frame.append("\n")

    def refresh(self, *, force=False):
        now = self.clock()
        if not force and now < self.next_refresh:
            return False
        frame = Text()
        while self.pending:
            item = self.pending[0]
            if isinstance(item, StreamBlock):
                style = "grey70" if item.event["type"] == "reasoning-delta" else "green"
                if not item.started and item.parts:
                    self._heading(frame, item.event)
                    request = safe_text(item.event["request_id"] or "-", self.secrets)[:8]
                    frame.append(f"{KINDS[item.event['type']]} · 请求 {request}\n", style)
                    item.started = True
                if item.parts:
                    text = "".join(item.parts)
                    frame.append(text, style)
                    item.newline = text.endswith("\n")
                    item.parts.clear()
                    item.size = 0
                if not item.closed:
                    break  # Other streams cannot interrupt an unfinished word/paragraph.
                if not item.newline:
                    frame.append("\n")  # UI delimiter, never inserted between deltas.
            else:
                self._metadata(frame, item)
            self.pending.popleft()
            if isinstance(item, StreamBlock):
                item.queued = False
        self.next_refresh = now + self.period
        if frame:
            self.console.print(frame, end="", soft_wrap=True)
        return bool(frame)

    def flush(self):
        for key in list(self.streams):
            self._seal(key)
        self.refresh(force=True)

    def warning(self, text):
        # Do not finalize a partial credential or join words across an observed gap.
        self._capacity_commit(announce=False)
        held = sum(len(block.redactor.pending) for block in self.streams.values())
        self.streams.clear()
        self.write(f"[观测丢失] {text}（省略 {held} 字符未确认尾部）", "red")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="本地 Live Monitor：只读观察，不控制 Agent")
    parser.add_argument("--once", action="store_true", help="显示一次后退出")
    parser.add_argument("--history", type=int, default=0, help="回看最近 0–200 个观测事件")
    parser.add_argument("--interval", type=float, default=0.2, help="文件轮询间隔，0.2–10 秒")
    parser.add_argument(
        "--refresh-rate", type=float, default=5, help="常规显示刷新上限，1–20 次/秒"
    )
    parser.add_argument("--no-color", action="store_true", help="禁用颜色，保留可读纯文本")
    parser.add_argument("--rows", type=int, default=3, help="每表显示最近 1–10 行")
    parser.add_argument("--verbose", action="store_true", help="另显示任务/模型状态、用量和决策")
    database_options = parser.add_mutually_exclusive_group()
    database_options.add_argument("--show-db", action="store_true", help="另显示只读数据库快照")
    database_options.add_argument("--no-db", action="store_true", help="兼容旧命令：不显示数据库")
    args = parser.parse_args()
    if (
        not 0 <= args.history <= 200
        or not 0.2 <= args.interval <= 10
        or not 1 <= args.rows <= 10
        or not 1 <= args.refresh_rate <= 20
    ):
        parser.error("刷新间隔或显示上限不合法")
    values = {**dotenv_values(Path.cwd() / ".env", interpolate=False), **os.environ}
    secrets = tuple(values.get(k, "") or "" for k in ("DISCORD_BOT_TOKEN", "DEEPSEEK_API_KEY"))
    directory = Path(values.get("MONITOR_DIRECTORY") or "data/monitor")
    lab = Path(values.get("DSH_LAB_PATH") or "../dsh-lab")
    social_db = Path(values.get("DATABASE_PATH") or "data/bot.sqlite3")
    personal_db = Path(values.get("PERSONAL_DATABASE_PATH") or "data/personal.sqlite3")
    tail = EventTail([directory, lab / "evidence/monitor"], history=args.history)
    renderer = Renderer(
        sys.stdout,
        secrets=secrets,
        refresh_rate=args.refresh_rate,
        no_color=args.no_color,
        verbose=args.verbose,
        show_db=args.show_db,
    )
    previous = None
    next_db = 0
    try:
        renderer.write("本地 Live Monitor｜只读观察｜关闭本窗口不会停止 Agent")
        renderer.write("推理、回答、工具与异常；--verbose 查看状态/用量，--show-db 查看数据库。")
        if args.history:
            renderer.write(f"回看最近 {args.history} 条事件；历史窗口可能从片段中间开始。")
        while True:
            events = tail.poll()
            if lost := tail.take_lost():
                renderer.warning(f"{lost} 条记录/文件因损坏、截断或未读轮换无法读取。")
            for event in events:
                renderer.event(event)
            if args.show_db and time.monotonic() >= next_db:
                next_db = time.monotonic() + max(1, args.interval)
                snapshot = {
                    "个人数据库": database_snapshot(
                        personal_db,
                        ("agent_messages", "agent_conversations"),
                        rows=args.rows,
                        secrets=secrets,
                    ),
                    "社交数据库": database_snapshot(
                        social_db,
                        ("messages", "social_decisions"),
                        rows=args.rows,
                        secrets=secrets,
                    ),
                }
                if snapshot != previous:
                    for event in snapshot_events(snapshot, secrets=secrets):
                        renderer.event(event)
                    previous = snapshot
            renderer.refresh()
            if args.once:
                break
            time.sleep(args.interval)
    except (KeyboardInterrupt, BrokenPipeError):
        return
    finally:
        try:
            renderer.flush()
        except (BrokenPipeError, OSError):
            pass


if __name__ == "__main__":
    main()
