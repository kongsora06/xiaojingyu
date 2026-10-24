"""Windows producer configuration; the monitor never becomes an execution dependency."""

import asyncio
import os
import subprocess
from contextvars import ContextVar
from pathlib import Path

from .monitor_protocol import FILE_NAME, EventSpool, Publisher
from .private_logs import WINDOWS_ACL_SCRIPT, _reject_links

scope = ContextVar("observation_scope", default=(None, None))


def prepare_directory(path):
    path = Path(path).absolute()
    _reject_links(path)
    if path.drive.startswith("\\\\") or path in {Path.cwd(), Path.home(), Path(path.anchor)}:
        raise OSError("Use a dedicated local monitor directory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    for entry in path.iterdir():
        if not FILE_NAME.fullmatch(entry.name) or entry.is_symlink() or not entry.is_file():
            raise OSError("Monitor directory contains unmanaged files")
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_ACL_SCRIPT],
                env={**os.environ, "DFY_PRIVATE_LOG_DIRECTORY": str(path)},
                capture_output=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.SubprocessError:
            raise OSError("Could not protect monitor directory") from None
        if result.returncode:
            raise OSError("Could not protect monitor directory")
    else:
        path.chmod(0o700)
    return path


class MonitorBus:
    def __init__(self, directory, secrets=()):
        self.directory, self.secrets = directory, secrets
        self.sink = None

    async def open(self):
        directory = await asyncio.to_thread(prepare_directory, self.directory)
        self.sink = EventSpool(directory)

    def publisher(self, source, *, session_id=None, task_id=None):
        return Publisher(
            self.sink, source, session_id=session_id, task_id=task_id, secrets=self.secrets
        )

    async def close(self):
        if self.sink:
            await asyncio.to_thread(self.sink.close)
