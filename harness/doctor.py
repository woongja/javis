"""`harness doctor [--probe] [--fix]`: is this machine ready? (portability core)"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from harness.paths import env_path, env_value, repo_root, state_dir
from harness import platform
from harness.platform import find_exe
from harness.roles import Role, lead_candidates
from harness.runner import safe_env

CODEX_MIN = (0, 171, 0)
_ICON = {"ok": "✅", "warn": "⚠", "fail": "✗"}


@dataclass
class Check:
    name: str
    status: Literal["ok", "warn", "fail"]
    detail: str
    fix: str | None = None


def parse_version(text: str) -> tuple[int, int, int]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _version_of(exe: Path) -> str:
    try:
        return subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=30,
                              env=safe_env(os.environ, {}),
                              encoding="utf-8", errors="replace").stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"error: {exc}"


def _git_config(root: Path, key: str) -> str:
    return subprocess.run(["git", "config", "--get", key], cwd=root, capture_output=True, text=True).stdout.strip()


def _env_keys(root: Path) -> Check:
    example = root / ".env.example"
    if not example.exists():
        return Check("env-keys", "ok", "no .env.example")
    wanted = [l.split("=", 1)[0].strip() for l in example.read_text(encoding="utf-8").splitlines()
              if "=" in l and not l.lstrip().startswith("#")]
    missing = [k for k in wanted if not env_value(root, k)]
    if missing:
        return Check("env-keys", "warn", f"not set: {', '.join(missing)}", "fill them in .env (never commit it)")
    return Check("env-keys", "ok", f"{len(wanted)} key(s) set")


def _brain_dir(root: Path) -> Check:
    path = env_path(root, "JAVIS_BRAIN_DIR")
    if path is None:
        return Check("brain-dir", "warn", "not set")
    if not path.is_dir():
        return Check("brain-dir", "fail", "JAVIS_BRAIN_DIR points to a missing folder", "fix the path in .env")
    return Check("brain-dir", "ok", "folder exists")


def _tool_checks() -> list[Check]:
    checks = []
    py = sys.version_info
    checks.append(Check("python>=3.12", "ok" if py >= (3, 12) else "fail", f"{py.major}.{py.minor}.{py.micro}",
                        None if py >= (3, 12) else "install Python 3.12+ (uv python install 3.12)"))
    for name, required, fix in (("uv", True, "https://docs.astral.sh/uv/ install"),
                                ("git", True, "install git"),
                                ("node", False, "install Node.js LTS (needed for the HUD later)")):
        found = find_exe(name)
        status = "ok" if found else ("fail" if required else "warn")
        checks.append(Check(name, status, str(found) if found else "not found", None if found else fix))
    claude = find_exe("claude", "JAVIS_CLAUDE_BIN")
    checks.append(Check("claude", "ok", _version_of(claude)) if claude else
                  Check("claude", "fail", "not found", "npm i -g @anthropic-ai/claude-code, then `claude` to log in"))
    codex = find_exe("codex", "JAVIS_CODEX_BIN")
    if not codex:
        checks.append(Check("codex>=0.171.0", "fail", "not found", "npm i -g @openai/codex@latest, then `codex login`"))
    else:
        text = _version_of(codex)
        ok = parse_version(text) >= CODEX_MIN
        checks.append(Check("codex>=0.171.0", "ok" if ok else "fail", text,
                            None if ok else "npm i -g @openai/codex@latest"))
    return checks


def run_checks(root: Path, probe: bool = False) -> list[Check]:
    checks = _tool_checks()
    hooks = _git_config(root, "core.hooksPath")
    checks.append(Check("hooksPath", "ok" if hooks == ".githooks" else "fail", hooks or "unset",
                        None if hooks == ".githooks" else "harness doctor --fix"))
    checks.append(_env_keys(root))
    checks.append(_brain_dir(root))
    has_eol = (root / ".gitattributes").exists()
    checks.append(Check("eol", "ok" if has_eol else "warn", ".gitattributes" if has_eol else "missing"))
    checks.append(_lead_check(root, probe))
    return checks


def _probe(role: Role, root: Path) -> bool:
    exe = find_exe(role.tool, f"JAVIS_{role.tool.upper()}_BIN")
    if not exe:
        return False
    if role.tool == "claude":
        cmd = [str(exe), "-p", "--model", role.model, "--effort", role.effort, "--setting-sources", "project,local"]
    else:
        cmd = [str(exe), "exec", "-m", role.model, "-c", f'model_reasoning_effort="{role.effort}"',
               "--ignore-user-config", "-s", "read-only", *platform.codex_sandbox_args(), "-"]
    try:
        proc = subprocess.run(cmd, input="Reply with OK", cwd=root, env=safe_env(os.environ, {}), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def select_lead(root: Path, probe_fn: Callable[[Role], bool]) -> Role | None:
    forced = os.environ.get("JAVIS_LEAD")
    for role in lead_candidates():
        if forced and role.tool != forced:
            continue
        if probe_fn(role):
            path = state_dir(root) / "local.toml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'lead = "{role.tool}/{role.model}/{role.effort}"\n', encoding="utf-8")
            return role
    return None


def _lead_check(root: Path, probe: bool) -> Check:
    if probe:
        role = select_lead(root, lambda r: _probe(r, root))
        if role:
            return Check("lead", "ok", f"open the lead session in {role.tool} ({role.model}, {role.effort})")
        return Check("lead", "fail", "no lead candidate answered", "log in to claude or codex, then --probe again")
    local = state_dir(root) / "local.toml"
    if local.exists():
        return Check("lead", "ok", local.read_text(encoding="utf-8").strip())
    return Check("lead", "warn", "not selected yet", "harness doctor --probe")


def _cmd_doctor(args: argparse.Namespace) -> int:
    root = repo_root()
    if args.fix:
        subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=root, check=True)
    checks = run_checks(root, probe=args.probe)
    for c in checks:
        hint = f"  ({c.fix})" if c.fix else ""
        print(f"{_ICON[c.status]} {c.name} — {c.detail}{hint}")
    return 1 if any(c.status == "fail" for c in checks) else 0


def register(subparsers) -> None:
    p = subparsers.add_parser("doctor", help="check this machine is ready")
    p.add_argument("--probe", action="store_true", help="make a tiny real call to pick the lead model")
    p.add_argument("--fix", action="store_true", help="apply safe fixes (git hooks path)")
    p.set_defaults(func=_cmd_doctor)
