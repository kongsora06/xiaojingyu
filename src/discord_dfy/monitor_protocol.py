"""Phase 3C v1 data-only protocol. Vendored unchanged into dsh-lab/scripts.

Only offer() runs on producer paths; disk I/O belongs to a bounded daemon writer.
This module never accepts execution/control commands or opens agent databases.
"""

import json
import queue
import re
import threading
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

SOURCES = {"social", "personal", "sqlite"}
TYPES = {
    "reasoning-delta",
    "text-delta",
    "tool-call",
    "tool-result",
    "task-status",
    "model-status",
    "usage",
    "decision",
    "database-snapshot",
    "dropped",
}
FILE_NAME = re.compile(r"^live-[a-f0-9]{32}\.jsonl$")
CREDENTIAL = re.compile(
    r"(?i)(?:authorization\s*[:=]\s*(?:bot|bearer)\s+\S+"
    r"|\bsk-[a-z0-9_-]{12,}|\bmfa\.[\w-]{20,}"
    r"|\b[\w-]{23,28}\.[\w-]{6}\.[\w-]{27,})"
)
MAX_EVENT_BYTES = 16384


class TerminalFilter:
    def __init__(self):
        self.state = "normal"

    def feed(self, text):
        output = []
        for ch in text:
            code = ord(ch)
            if self.state in {"osc", "string"}:
                if ch == "\x07" or code == 0x9C:
                    self.state = "normal"
                elif ch == "\x1b":
                    self.state = "string-escape"
                continue
            if self.state == "string-escape":
                self.state = "normal" if ch == "\\" else "string"
                continue
            if self.state == "csi":
                if 0x40 <= code <= 0x7E:
                    self.state = "normal"
                continue
            if self.state == "escape":
                self.state = (
                    "csi"
                    if ch == "["
                    else "osc"
                    if ch == "]"
                    else ("string" if ch in "P^_" else "normal")
                )
                continue
            if ch == "\x1b":
                self.state = "escape"
            elif code == 0x9B:
                self.state = "csi"
            elif code == 0x9D:
                self.state = "osc"
            elif ch in "\n\t" or (
                code >= 32
                and not 0x7F <= code <= 0x9F
                and not 0x202A <= code <= 0x202E
                and not 0x2066 <= code <= 0x2069
            ):
                output.append(ch)
        return "".join(output)


def safe_text(text, secrets=()):
    text = TerminalFilter().feed(str(text))
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return CREDENTIAL.sub("[REDACTED]", text)


class StreamRedactor:
    """Retain a bounded suffix so credentials/control sequences can span chunks."""

    def __init__(self, secrets=()):
        self.secrets = tuple(s for s in secrets if s)
        self.keep = max([80, *(len(s) for s in self.secrets)])
        self.pending = ""
        self.terminal = TerminalFilter()
        self.discard_token = False

    def feed(self, text, *, final=False):
        text = self.terminal.feed(text)
        if self.discard_token:
            split = re.search(r"\s", text)
            if split is None:
                return ""
            text = text[split.start() :]
            self.discard_token = False
        self.pending += text
        cut = len(self.pending) if final else max(0, len(self.pending) - self.keep)
        if not final:
            spans = [(m.start(), m.end()) for m in CREDENTIAL.finditer(self.pending)]
            for secret in self.secrets:
                start = 0
                while (start := self.pending.find(secret, start)) >= 0:
                    spans.append((start, start + len(secret)))
                    start += len(secret)
            for start, end in sorted(spans):
                if start < cut < end:
                    cut = start
        if len(self.pending) > 8192 and cut == 0:
            self.pending = ""
            self.discard_token = True
            return "[REDACTED]"
        result = safe_text(self.pending[:cut], self.secrets)
        self.pending = self.pending[cut:]
        return result


def validate_event(event):
    if not isinstance(event, dict) or set(event) != {
        "v",
        "source",
        "type",
        "timestamp",
        "session_id",
        "task_id",
        "request_id",
        "data",
    }:
        return False
    if (
        type(event["v"]) is not int
        or event["v"] != 1
        or not isinstance(event["source"], str)
        or event["source"] not in SOURCES
        or not isinstance(event["type"], str)
        or event["type"] not in TYPES
    ):
        return False
    if not isinstance(event["timestamp"], str) or len(event["timestamp"]) > 40:
        return False
    if any(
        v is not None and (not isinstance(v, str) or len(v) > 128)
        for v in (event["session_id"], event["task_id"], event["request_id"])
    ):
        return False
    return (
        isinstance(event["data"], dict)
        and len(event["data"]) <= 12
        and all(
            key not in event["data"] or isinstance(event["data"][key], str)
            for key in ("text", "status", "name")
        )
        and all(
            isinstance(key, str)
            and len(key) <= 80
            and type(value) in {str, int, float, bool, type(None)}
            for key, value in event["data"].items()
        )
    )


class EventSpool:
    """Lossy observer: bounded queue, bounded files, no dependency on readers."""

    def __init__(self, directory, *, capacity=128, file_bytes=1024 * 1024, retained=8):
        self.directory = Path(directory)
        self.queue = queue.Queue(maxsize=capacity)
        self.file_bytes = file_bytes
        self.retained = retained
        self.dropped = 0
        self.closed = False
        self.thread = threading.Thread(target=self._write, daemon=True, name="dfy-observation")
        self.thread.start()

    def offer(self, event):
        if self.closed or not validate_event(event):
            return
        try:
            body = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            if len(body) > MAX_EVENT_BYTES:
                self.dropped += 1
                return
            self.queue.put_nowait(body)
        except (queue.Full, ValueError, UnicodeError):
            self.dropped += 1

    def _write(self):
        target = None
        size = 0
        while not self.closed or not self.queue.empty():
            try:
                body = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if target is None or size + len(body) > self.file_bytes:
                    target = self.directory / f"live-{uuid.uuid4().hex}.jsonl"
                    # Do not create directories or follow untrusted links on producer threads.
                    target.touch(mode=0o600, exist_ok=False)
                    size = 0
                    files = sorted(
                        (
                            p
                            for p in self.directory.iterdir()
                            if FILE_NAME.fullmatch(p.name) and p != target and not p.is_symlink()
                        ),
                        key=lambda p: p.stat().st_mtime_ns,
                    )
                    for old in files[: max(0, len(files) - (self.retained - 1))]:
                        try:
                            old.unlink()
                        except OSError:
                            pass
                with target.open("ab") as stream:
                    stream.write(body)
                size += len(body)
            except OSError:
                self.dropped += 1
                target = None

    def close(self):
        self.closed = True
        self.thread.join(timeout=1)


class Publisher:
    def __init__(self, sink, source, *, session_id=None, task_id=None, secrets=()):
        self.sink, self.source = sink, source
        self.session_id, self.task_id = session_id, task_id
        self.secrets = secrets
        self.streams = OrderedDict()

    def event(self, kind, *, request_id=None, **data):
        try:
            cleaned = {}
            for key, value in list(data.items())[:12]:
                if isinstance(value, str):
                    value = safe_text(value, self.secrets)[:2048]
                elif type(value) not in {int, float, bool, type(None)}:
                    continue
                cleaned[key] = value
            self.sink.offer(
                {
                    "v": 1,
                    "source": self.source,
                    "type": kind,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "session_id": self.session_id,
                    "task_id": self.task_id,
                    "request_id": request_id,
                    "data": cleaned,
                }
            )
        except Exception:
            pass  # Observation failures must never change the task result.

    def text(self, kind, text="", *, request_id=None, final=False):
        key = (request_id, kind)
        if key not in self.streams:
            if len(self.streams) >= 64:
                self.streams.popitem(last=False)
            self.streams[key] = StreamRedactor(self.secrets)
        redactor = self.streams[key]
        # Bound temporary allocations for large external fragments.
        for offset in range(0, max(1, len(text)), 2048):
            part = redactor.feed(
                text[offset : offset + 2048], final=final and offset + 2048 >= len(text)
            )
            for start in range(0, len(part), 1024):
                self.event(kind, request_id=request_id, text=part[start : start + 1024])
        if final:
            self.streams.pop(key, None)

    def flush(self):
        for request_id, kind in list(self.streams):
            self.text(kind, request_id=request_id, final=True)
