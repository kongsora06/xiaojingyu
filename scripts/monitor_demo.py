"""Synthetic Rich viewer demo. No bot, model, database, or sandbox is started."""

import argparse
import sys
from pathlib import Path

from rich.console import Console

from discord_dfy.monitor import Renderer


def main():
    parser = argparse.ArgumentParser(description="示例：合成 Terminal Monitor 事件")
    parser.add_argument("--html", type=Path, help="另存彩色示例 HTML")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    console = Console(record=True, markup=False, highlight=False, emoji=False)
    renderer = Renderer(sys.stdout, console=console)
    renderer.write("示例：以下全部是合成事件，不是真实模型推理或执行结果。", "bold blue")

    def emit(kind, **data):
        renderer.event(
            {
                "v": 1,
                "source": "personal",
                "type": kind,
                "timestamp": "2026-10-22T14:32:05+00:00",
                "task_id": "demo-001-task",
                "session_id": "demo-001-session",
                "request_id": "demo-001-request",
                "data": data,
            }
        )

    emit("task-status", status="running")
    for part in (
        "我先检查函数和测试。\n",
        "  Deep",
        "Seek",
        " Harness",
        " 的文字片段会连续拼接。🙂\n",
    ):
        emit("reasoning-delta", text=part)
    emit("model-status", status="completed")
    emit("tool-call", name="Shell", text="python -m pytest -q")
    emit("tool-result", status="returned", text="示例结果：5 passed")
    for part in ("已修复 ", "calculator.py", "，测试全部通过。\n"):
        emit("text-delta", text=part)
    emit("task-status", status="completed")
    emit("tool-result", status="error", text="示例错误：此行只用于演示红色显示。")
    renderer.flush()
    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        html = console.export_html(inline_styles=True)
        html = html.replace("<title>Rich</title>", "<title>合成示例：Live Monitor</title>")
        html = html.replace(
            '<pre style="', '<pre style="white-space:pre-wrap;overflow-wrap:anywhere;'
        )
        args.html.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
