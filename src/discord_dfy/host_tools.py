"""Owner-only tools that act directly on this Windows computer. No sandbox, no allowlist."""

import asyncio
import base64
import json
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MAX_OUTPUT_CHARS = 30000
MAX_SEND_BYTES = 10 * 1024 * 1024

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "powershell",
            "description": (
                "在老爹的 Windows 电脑上执行一条 PowerShell 命令"
                "（pwsh，找不到时用 Windows PowerShell）。"
                "同一个 shell 常驻：cd、变量、函数会保留到下一次调用（!new 或超时后重置）。"
                "可选参数 cwd 只在这一次调用里临时切换目录，结束后回到原目录；"
                "命令里自己执行的 cd 会保留。stdout 与 stderr 合并返回，过长时只保留首尾。"
                "不要运行等待键盘输入的命令；长时间运行的服务用 Start-Process 放到后台。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 PowerShell 脚本"},
                    "cwd": {"type": "string", "description": "可选：仅本次调用临时使用的目录"},
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "超时秒数，超时后终止整个进程树并重置 shell",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文本文件，返回带行号的内容。大文件用 offset/limit 分段读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "起始行号，从 1 开始"},
                    "limit": {"type": "integer", "description": "最多读取的行数，默认 2000"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "写入完整文件内容（覆盖已有文件，自动创建上级目录）。局部修改优先用 edit_file。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "把文件中的 old_string 精确替换为 new_string。old_string 必须唯一匹配，"
                "否则需要提供更多上下文或设置 replace_all。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": (
                "把本机文件（截图、日志、生成的产物等，最大 10 MB）作为附件发到当前私聊。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
]

WEB_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "上网搜索，返回标题、网址、日期和摘要。查新闻、最新信息、你不确定的事实时用。"
                '支持 site:、-site:、"精确短语"、lang:zh、after:2026-01-01 等写法。'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词，最多 128 字"},
                    "count": {"type": "integer", "description": "结果条数，1～10，默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "打开一个网页，把正文转成 Markdown 返回。看搜索结果的全文或者老爹发来的链接时用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
]

VIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "view_image",
        "description": (
            "看一张本机图片（PNG/JPEG/GIF/WebP，最大 8 MB），比如截图后查看屏幕、看生成的图表。"
            "图片会在工具结果之后作为一条宿主消息附给你。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}

PROFILE_TOOL = {
    "type": "function",
    "function": {
        "name": "update_profile",
        "description": (
            "改你自己的 Discord 资料。field=status：改状态文字（value，留空就是清掉）和在线状态"
            "（state：online 在线、idle 离开、dnd 请勿打扰），这个可以随时按心情自己改。"
            "其他 field 只有老爹在私聊里明确同意了才能改，先问他："
            "avatar / banner（value 是本机图片路径；可以先下载、用 view_image 看过再换）、"
            "username（全局名字，一小时最多改两次）、about（个人简介 About Me）、"
            "nickname（在群里的昵称，留空恢复默认）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "enum": ["status", "avatar", "banner", "username", "about", "nickname"],
                },
                "value": {"type": "string"},
                "state": {"type": "string", "enum": ["online", "idle", "dnd"]},
            },
            "required": ["field"],
        },
    },
}

RESTART_TOOL = {
    "type": "function",
    "function": {
        "name": "restart_bot",
        "description": (
            "重启 discord-dfy bot，让刚修改的 bot 自身代码或 .env 生效。"
            "当前这一步结束后重启，重启后会在私信里报到。先确认测试通过再用。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

GROUP_READ_TOOL = {
    "type": "function",
    "function": {
        "name": "group_read",
        "description": (
            "看群聊频道最近的聊天记录（最近两小时内，最多 50 条，含你自己在群里说的话）。"
            "老爹问起群里的事、或者你想知道群里刚才在聊什么时用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "频道名、频道 ID 或服务器名；只有一个群聊频道时可以省略",
                },
            },
        },
    },
}

GROUP_TOOL = {
    "type": "function",
    "function": {
        "name": "group_post",
        "description": (
            "以你自己的身份在 Discord 群聊频道里发一条消息，群里的人会看到是你说的。"
            "只在老爹让你去群里说什么、转告什么时用；你自己想去群里说话先问老爹。"
            "用你在群里说话的口吻写。之后群友接你的话，你在群里会接着聊。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要发的内容"},
                "channel": {
                    "type": "string",
                    "description": "频道名、频道 ID 或服务器名；只有一个群聊频道时可以省略",
                },
            },
            "required": ["text"],
        },
    },
}


class ToolError(Exception):
    """A tool failure reported back to the model as data."""


# Runs inside one long-lived shell. Each request is "<marker> <base64 script>"; the
# script is dot-sourced so cd/variables/functions persist, then the marker line ends it.
DRIVER = r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8;$OutputEncoding=[Text.Encoding]::UTF8
$ProgressPreference='SilentlyContinue'
while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) { break }
  $marker, $payload = $line.Split(' ', 2)
  $code = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($payload))
  $global:LASTEXITCODE = 0
  try { . ([ScriptBlock]::Create($code)) 2>&1 | Out-String -Stream -Width 250 }
  catch { $_ | Out-String -Width 250 }
  [Console]::Out.WriteLine("$marker $global:LASTEXITCODE")
}
"""
MAX_BUFFER_BYTES = 2 * 1024 * 1024


@dataclass
class HostTools:
    workdir: Path
    command_timeout: int = 300
    shell: str = field(default_factory=lambda: shutil.which("pwsh") or "powershell.exe")
    process: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def resolve(self, path: str) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ToolError("path 不能为空")
        target = Path(os.path.expandvars(os.path.expanduser(path.strip())))
        return target if target.is_absolute() else self.workdir / target

    async def _start(self):
        self.process = await asyncio.create_subprocess_exec(
            self.shell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            DRIVER,
            cwd=self.workdir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    async def reset_shell(self):
        """Kill the shell and everything it started; the next command gets a fresh one."""
        process, self.process = self.process, None
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            await kill_tree(process)

    async def powershell(self, command: str, cwd: str | None = None, timeout_seconds=None) -> str:
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command 不能为空")
        if cwd:
            directory = self.resolve(cwd)
            if not directory.is_dir():
                raise ToolError(f"目录不存在：{directory}")
            quoted = str(directory).replace("'", "''")
            command = (
                f"Push-Location -LiteralPath '{quoted}'\ntry {{\n{command}\n}} "
                "finally { Pop-Location }"
            )
        timeout = self.command_timeout
        if isinstance(timeout_seconds, int) and timeout_seconds > 0:
            timeout = min(timeout_seconds, 3600)
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                await self._start()
            process = self.process
            marker = secrets.token_hex(16)
            payload = base64.b64encode(command.encode("utf-8")).decode("ascii")
            output = bytearray()
            dropped = 0
            note = ""
            code = None
            try:
                process.stdin.write(f"{marker} {payload}\n".encode("ascii"))
                await process.stdin.drain()
                async with asyncio.timeout(timeout):
                    while True:
                        chunk = await process.stdout.read(65536)
                        if not chunk:
                            note = "[shell 已退出，下次调用会开一个新的；之前的 cd/变量已失效]"
                            await self.reset_shell()
                            break
                        output += chunk
                        index = output.find(marker.encode("ascii"))
                        if index >= 0 and b"\n" in output[index:]:
                            code = output[index:].split(b"\n", 1)[0].split(b" ")[-1].strip()
                            del output[index:]
                            break
                        if len(output) > MAX_BUFFER_BYTES:
                            # Keep enough tail to still see a marker split across reads.
                            cut = len(output) - MAX_BUFFER_BYTES // 2
                            dropped += cut
                            del output[:cut]
            except TimeoutError:
                await self.reset_shell()
                note = f"[超时：{timeout} 秒后已终止进程树；shell 已重置，之前的 cd/变量已失效]"
            except asyncio.CancelledError:
                await self.reset_shell()
                raise
            except (BrokenPipeError, ConnectionResetError):
                await self.reset_shell()
                note = "[shell 意外退出，已重置；请重试]"
        text = output.decode("utf-8", errors="replace").replace("\r\n", "\n").rstrip()
        if dropped:
            text = f"[前面省略 {dropped} 字节]\n{text}"
        status = f"exit_code={code.decode('ascii', 'replace')}" if code is not None else note
        return f"{status}\n{clip(text)}" if text else status

    async def close(self):
        await self.reset_shell()

    def read_file(self, path: str, offset=1, limit=2000) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise ToolError(f"文件不存在：{target}")
        offset = max(1, offset if isinstance(offset, int) else 1)
        limit = max(1, min(limit if isinstance(limit, int) else 2000, 5000))
        with target.open(encoding="utf-8", errors="replace") as stream:
            lines = stream.read().splitlines()
        selected = lines[offset - 1 : offset - 1 + limit]
        body = "\n".join(f"{number}\t{line}" for number, line in enumerate(selected, offset))
        more = len(lines) - (offset - 1 + len(selected))
        suffix = f"\n[还有 {more} 行未显示，共 {len(lines)} 行]" if more > 0 else ""
        return clip(body + suffix) if body else f"[空或超出范围；共 {len(lines)} 行]"

    def write_file(self, path: str, content: str) -> str:
        if not isinstance(content, str):
            raise ToolError("content 必须是字符串")
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as stream:
            stream.write(content)
        return f"已写入 {target}（{len(content)} 字符）"

    def edit_file(self, path: str, old_string: str, new_string: str, replace_all=False) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise ToolError(f"文件不存在：{target}")
        if not isinstance(old_string, str) or not old_string or not isinstance(new_string, str):
            raise ToolError("old_string 不能为空，new_string 必须是字符串")
        with target.open(encoding="utf-8", newline="") as stream:
            text = stream.read()
        count = text.count(old_string)
        if count == 0 and "\r\n" in text and "\r\n" not in old_string:
            # The model writes \n; Windows files often use CRLF.
            old_string = old_string.replace("\n", "\r\n")
            new_string = new_string.replace("\n", "\r\n")
            count = text.count(old_string)
        if count == 0:
            raise ToolError("没有找到 old_string，请先 read_file 确认原文")
        if count > 1 and replace_all is not True:
            raise ToolError(f"old_string 匹配到 {count} 处，请补充上下文或设置 replace_all")
        text = (
            text.replace(old_string, new_string)
            if replace_all is True
            else text.replace(old_string, new_string, 1)
        )
        with target.open("w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        return f"已修改 {target}（替换 {count if replace_all is True else 1} 处）"

    def check_send(self, path: str) -> Path:
        target = self.resolve(path)
        if not target.is_file():
            raise ToolError(f"文件不存在：{target}")
        if target.stat().st_size > MAX_SEND_BYTES:
            raise ToolError("文件超过 10 MB，Discord 无法直接发送")
        return target


def clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[... 省略 {len(text) - limit} 字符 ...]\n{text[-half:]}"


async def kill_tree(process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        # Kill children too; process.kill() would leave them running.
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/T",
            "/F",
            "/PID",
            str(process.pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        await killer.wait()
    else:
        process.kill()
    try:
        await asyncio.wait_for(process.wait(), 10)
    except TimeoutError:
        process.kill()
        await process.wait()


def describe_call(name: str, arguments: dict) -> str:
    """One-line human summary of a tool call for the Discord progress trail."""
    if name == "powershell":
        detail = arguments.get("command", "")
    elif name == "edit_file" or name == "write_file" or name == "read_file":
        detail = arguments.get("path", "")
    elif name == "send_file":
        detail = arguments.get("path", "")
    elif name == "update_profile":
        detail = f"{arguments.get('field', '')} {arguments.get('value') or ''}"
    elif name == "web_search":
        detail = arguments.get("query", "")
    elif name == "web_fetch":
        detail = arguments.get("url", "")
    elif name == "view_image":
        detail = arguments.get("path", "")
    elif name == "group_post":
        detail = f"{arguments.get('channel') or ''} {arguments.get('text', '')}"
    else:
        detail = json.dumps(arguments, ensure_ascii=False)
    detail = " ".join(str(detail).split()).replace("`", "'")
    if len(detail) > 160:
        detail = detail[:157] + "..."
    return f"`{name}` {detail}".rstrip()
