"""The only place that branches on the operating system."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def is_windows() -> bool:
    return sys.platform == "win32"


def codex_sandbox_args() -> list[str]:
    """Extra `codex exec` args for this OS.

    `--ignore-user-config` also drops the Windows sandbox setting; without it every
    command is rejected ("blocked by policy") even under workspace-write (seen 2026-11-20).
    """
    return ["-c", 'windows.sandbox="elevated"'] if is_windows() else []


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if is_windows():
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def find_exe(name: str, env_override: str | None = None) -> Path | None:
    """Locate an executable. An override env var (e.g. JAVIS_CODEX_BIN) wins.

    On Windows shutil.which resolves npm `.cmd` shims, so callers can run the
    returned path with shell=False.
    """
    if env_override:
        override = os.environ.get(env_override)
        if override:
            return Path(override)
    found = shutil.which(name)
    return Path(found) if found else None
