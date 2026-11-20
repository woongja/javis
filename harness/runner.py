"""Build and run `claude -p` / `codex exec` for a role.

R5  child processes get an allow-listed environment only.
    why: API keys in the parent env would switch billing off the subscription and
    leak secrets into agent runs. expires: never.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from harness import HarnessError
from harness.paths import state_dir
from harness import platform
from harness.platform import find_exe, pid_alive
from harness.roles import Role

ENV_ALLOWLIST = (
    "PATH", "PATHEXT", "HOME", "USERPROFILE", "USERNAME", "USER", "LOGNAME", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "TERM",
    "SHELL", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "CODEX_HOME",
    # harness's own variables, by name (never a JAVIS_* wildcard: it could carry secrets)
    "JAVIS_AGENT", "JAVIS_WORKTREE", "JAVIS_TASK", "JAVIS_MERGE", "JAVIS_FAKE_MODE", "JAVIS_FAKE_VERSION",
    "JAVIS_FAKE_LINK_TARGET",
)
_QUOTA = re.compile(r"usage limit|rate limit|quota|too many requests", re.I)


@dataclass
class RunResult:
    exit_code: int
    duration_s: float
    quota_hit: bool
    timed_out: bool


def safe_env(environ: Mapping[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    allowed = {k.upper() for k in ENV_ALLOWLIST}
    env = {k: v for k, v in environ.items() if k.upper() in allowed}
    env.update(extra)
    return env


def build_command(role: Role, worktree: Path) -> list[str]:
    exe = find_exe(role.tool, f"JAVIS_{role.tool.upper()}_BIN")
    if exe is None:
        raise HarnessError(f"{role.tool} not found on PATH (run `harness doctor`)")
    if role.tool == "claude":
        return [str(exe), "-p", "--model", role.model, "--effort", role.effort,
                "--setting-sources", "project,local", "--permission-mode", "acceptEdits"]
    return [str(exe), "exec", "-m", role.model, "-c", f'model_reasoning_effort="{role.effort}"',
            "--ignore-user-config", "-C", str(worktree), "-s", "workspace-write",
            *platform.codex_sandbox_args(), "-"]


def run(cmd: list[str], prompt: str, cwd: Path, env: dict[str, str], timeout_s: int, log_path: Path) -> RunResult:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, input=prompt, cwd=cwd, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout_s, shell=False)
    except subprocess.TimeoutExpired as exc:
        log_path.write_text(f"TIMEOUT after {timeout_s}s\n{exc.stdout or ''}", encoding="utf-8")
        return RunResult(-1, time.monotonic() - started, False, True)
    output = f"{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    log_path.write_text(output, encoding="utf-8")
    quota = proc.returncode != 0 and bool(_QUOTA.search(output))
    return RunResult(proc.returncode, round(time.monotonic() - started, 2), quota, False)


@contextmanager
def acquire_lock(root: Path, tool: str) -> Iterator[None]:
    """One run per tool at a time. A lock whose PID is dead is reclaimed."""
    lock = state_dir(root) / "locks" / f"{tool}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            holder = lock.read_text(encoding="utf-8").strip()
            if holder.isdigit() and pid_alive(int(holder)):
                raise HarnessError(f"busy: {tool} (pid {holder} is running)")
            lock.unlink(missing_ok=True)
    else:
        raise HarnessError(f"busy: {tool} (could not take lock)")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)
