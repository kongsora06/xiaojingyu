"""Keep the bot running: restart on crash or owner request, stop on a clean exit.

Run from the project root (`pythonw -m discord_dfy.supervisor`). Output goes to
data/logs/. Everything the bot starts lives in a Windows job object, so stopping the
supervisor also stops the bot and any shell it left behind.
"""

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RESTART_EXIT_CODE = 75
STABLE_SECONDS = 300
MAX_BACKOFF_SECONDS = 300


def log_path() -> Path:
    directory = Path("data/logs")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"bot-{datetime.now():%Y-%m-%d}.log"


def note(text: str) -> None:
    with log_path().open("a", encoding="utf-8") as stream:
        stream.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} SUPERVISOR {text}\n")


def single_instance():
    """Hold an exclusive lock for the supervisor's lifetime; None if another one runs."""
    path = Path("data/supervisor.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+")
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        return None
    return stream


def kill_on_close_job():
    """A job object whose processes all die when this process exits (Windows only)."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in "abcdef"]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", Basic),
            ("IoInfo", Counters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not job or not kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(info), ctypes.sizeof(info)
    ):
        return None
    return lambda process: kernel32.AssignProcessToJobObject(job, int(process._handle))


def wait_for_lock(sleep=time.sleep, interval: float = 15.0):
    """Become the supervisor, waiting while another one runs so we can take over if it dies."""
    lock = single_instance()
    if lock is None:
        note("another supervisor is running; waiting to take over")
    while lock is None:
        sleep(interval)
        lock = single_instance()
    return lock


def main() -> None:
    lock = wait_for_lock()  # noqa: F841 - held open for the supervisor's lifetime
    assign = kill_on_close_job()
    backoff = 5
    python = Path(sys.executable)
    # pythonw has no console; the bot itself runs under python.exe without a window.
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    while True:
        started = time.monotonic()
        # Pipe and copy the output ourselves: a child given the file handle would not
        # append on Windows and would overwrite these supervisor notes.
        process = subprocess.Popen(
            [str(python), "-m", "discord_dfy"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={**os.environ, "DFY_SUPERVISED": "1", "PYTHONUTF8": "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if assign is not None:
            assign(process)
        Path("data/persona-bot.pid").write_text(str(process.pid))
        note(f"started bot pid={process.pid}")
        for line in process.stdout:
            with log_path().open("ab") as output:
                output.write(line)
        code = process.wait()
        note(f"bot exited code={code}")
        if code == RESTART_EXIT_CODE:
            backoff = 5
            continue
        if code == 0:
            note("clean shutdown; supervisor stopping")
            return
        if time.monotonic() - started > STABLE_SECONDS:
            backoff = 5
        note(f"restarting in {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)


if __name__ == "__main__":
    main()
