import asyncio
import json
import os
import subprocess
import time
import uuid

import pytest

from discord_dfy.private_logs import PrivateReasoningLog


async def test_private_trace_redaction_acl_and_retention(tmp_path):
    directory = tmp_path / "private-reasoning"
    writer = PrivateReasoningLog(directory, retention_days=1, secrets=("secret-test-token",))
    await writer.open()
    try:
        identifier = uuid.uuid4().hex
        await writer.write(
            identifier,
            {
                "reasoning_content": "secret-test-token sk-abcdefghijklmnop",
                "final_content": "test",
                "action_result": "dry_run",
                "token_usage": {"total_tokens": 5},
            },
        )
        path = directory / f"{identifier}.reasoning.json"
        body = path.read_text(encoding="utf-8")
        assert "secret-test-token" not in body and "sk-abcdefghijklmnop" not in body
        assert json.loads(body)["action_result"] == "dry_run"
        # The final outcome replaces the same record atomically, rather than growing duplicate logs.
        await writer.write(identifier, {"action_result": "executed"})
        assert len(list(directory.iterdir())) == 1
        assert json.loads(path.read_text(encoding="utf-8"))["action_result"] == "executed"
        if os.name == "nt":
            script = r"""
$ErrorActionPreference = 'Stop'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$dirAcl = [IO.Directory]::GetAccessControl($env:DFY_ACL_TEST_DIRECTORY)
if (-not $dirAcl.AreAccessRulesProtected) { exit 3 }
foreach ($acl in @($dirAcl, [IO.File]::GetAccessControl($env:DFY_ACL_TEST_FILE))) {
    $rules = $acl.GetAccessRules($true, $true,
        [Security.Principal.SecurityIdentifier])
    if ($rules.Count -lt 1) { exit 4 }
    foreach ($rule in $rules) {
        if ($rule.IdentityReference.Value -ne $sid) { exit 5 }
    }
}
"""
            check = await asyncio.to_thread(
                subprocess.run,
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                env={
                    **os.environ,
                    "DFY_ACL_TEST_DIRECTORY": str(directory),
                    "DFY_ACL_TEST_FILE": str(path),
                },
                capture_output=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            assert check.returncode == 0
        else:
            assert directory.stat().st_mode & 0o777 == 0o700
            assert path.stat().st_mode & 0o777 == 0o600
        old = time.time() - 86401
        os.utime(path, (old, old))
        await writer.purge()
        assert not path.exists()
    finally:
        await writer.close()


async def test_unrelated_directory_is_never_repermissioned_or_cleaned(tmp_path):
    marker = tmp_path / "keep.txt"
    marker.write_text("preserve me", encoding="utf-8")
    writer = PrivateReasoningLog(tmp_path, retention_days=1)
    with pytest.raises(OSError, match="unmanaged"):
        await writer.open()
    assert marker.read_text(encoding="utf-8") == "preserve me"


async def test_non_generated_trace_id_is_rejected(tmp_path):
    writer = PrivateReasoningLog(tmp_path / "private", retention_days=1)
    await writer.open()
    try:
        with pytest.raises(ValueError):
            await writer.write("../outside", {"reasoning": "private"})
        assert not list((tmp_path / "private").iterdir())
    finally:
        await writer.close()
