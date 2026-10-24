"""Opt-in, owner-only, local reasoning traces. Never used as an execution input."""

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from .redaction import redact

log = logging.getLogger(__name__)
TRACE_NAME = re.compile(r"^[0-9a-f]{32}\.reasoning\.json$")

# A fresh, protected ACL replaces inherited/broad permissions on this dedicated directory.
# No string interpolation or shell quoting of user paths; the path travels as an env value.
WINDOWS_ACL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$root = $env:DFY_PRIVATE_LOG_DIRECTORY
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetOwner($sid)
$acl.SetAccessRuleProtection($true, $false)
$rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl',
    'ContainerInherit,ObjectInherit', 'None', 'Allow')
$acl.AddAccessRule($rule)
[IO.Directory]::SetAccessControl($root, $acl)
foreach ($file in [IO.Directory]::GetFiles($root)) {
    $fileAcl = New-Object Security.AccessControl.FileSecurity
    $fileAcl.SetOwner($sid)
    $fileAcl.SetAccessRuleProtection($true, $false)
    $fileAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
        $sid, 'FullControl', 'Allow')))
    [IO.File]::SetAccessControl($file, $fileAcl)
}
"""


def _reject_links(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink() or component.is_junction():
            raise OSError("Reasoning log paths must not contain links or junctions")


class PrivateReasoningLog:
    def __init__(
        self, directory: Path, *, retention_days: int, secrets: tuple[str, ...] = ()
    ) -> None:
        self.directory = directory.absolute()
        self._retention_seconds = retention_days * 86400
        self._secrets = secrets
        self._lock = asyncio.Lock()
        self._maintenance: asyncio.Task | None = None

    def _prepare(self) -> None:
        _reject_links(self.directory)
        if self.directory.drive.startswith("\\\\"):
            raise OSError("Reasoning logs require a local directory")
        if self.directory in {Path.cwd(), Path.home(), Path(self.directory.anchor)}:
            raise OSError("Use a dedicated reasoning log directory")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Do not alter permissions on an unrelated directory or its files.
        for entry in self.directory.iterdir():
            if not TRACE_NAME.fullmatch(entry.name) or not entry.is_file() or entry.is_symlink():
                raise OSError("Reasoning log directory contains unmanaged files")
        if os.name == "nt":
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_ACL_SCRIPT],
                env={**os.environ, "DFY_PRIVATE_LOG_DIRECTORY": str(self.directory)},
                capture_output=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode:
                raise OSError("Could not restrict reasoning log directory access")
        else:
            self.directory.chmod(0o700)
            for entry in self.directory.iterdir():
                entry.chmod(0o600)

    async def open(self) -> None:
        await asyncio.to_thread(self._prepare)
        await self.purge()
        self._maintenance = asyncio.create_task(self._maintain(), name="reasoning-log-retention")

    async def _maintain(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                await self.purge()
            except OSError:
                log.error("Reasoning log retention failed")

    def _purge(self, now: float) -> None:
        _reject_links(self.directory)
        for entry in self.directory.iterdir():
            if TRACE_NAME.fullmatch(entry.name) and not entry.is_symlink() and entry.is_file():
                # Delete only generated files whose resolved parent is the dedicated directory.
                if entry.resolve().parent != self.directory.resolve():
                    raise OSError("Invalid reasoning log path")
                if now - entry.stat().st_mtime >= self._retention_seconds:
                    entry.unlink()

    async def purge(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._purge, time.time())

    def _write(self, decision_id: str, record: dict) -> None:
        _reject_links(self.directory)
        if not re.fullmatch(r"[0-9a-f]{32}", decision_id):
            raise ValueError("Invalid private trace ID")
        target = self.directory / f"{decision_id}.reasoning.json"
        if target.is_symlink():
            raise OSError("Invalid private trace path")
        temporary = self.directory / f"{uuid.uuid4().hex}.reasoning.json"

        def sanitize(value):
            if isinstance(value, str):
                return redact(value, self._secrets)
            if isinstance(value, dict):
                return {sanitize(key): sanitize(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [sanitize(item) for item in value]
            return value

        body = json.dumps(sanitize(record), ensure_ascii=True, indent=2)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(body)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    async def write(self, decision_id: str, record: dict) -> None:
        async with self._lock:
            await asyncio.to_thread(self._purge, time.time())
            await asyncio.to_thread(self._write, decision_id, record)

    async def close(self) -> None:
        if self._maintenance is not None:
            self._maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._maintenance
            self._maintenance = None
