"""Hook entry points: Claude Code PreToolUse, git pre-commit, CI scan."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from harness.guards import GuardContext, check_command, check_path, scan_text
from harness.paths import repo_root

_FILE_TOOLS = {"Read": "read", "Write": "write", "Edit": "write", "MultiEdit": "write", "NotebookEdit": "write"}
_SEARCH_TOOLS = {"Grep", "Glob"}


def claude_pre(payload: dict, ctx: GuardContext) -> tuple[int, str]:
    tool = payload.get("tool_name", "")
    data = payload.get("tool_input") or {}
    verdict = None
    if tool in _FILE_TOOLS and data.get("file_path"):
        verdict = check_path(Path(data["file_path"]), _FILE_TOOLS[tool], ctx)
    elif tool == "Bash" and data.get("command"):
        verdict = check_command(data["command"], ctx)
    elif tool in _SEARCH_TOOLS and data.get("path"):
        verdict = check_path(Path(data["path"]), "read", ctx)
    if verdict is None or verdict.allowed:
        return 0, ""
    return 2, f"{verdict.rule}: {verdict.reason}"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout


def git_pre_commit(repo: Path, env: Mapping[str, str]) -> tuple[int, str]:
    if env.get("JAVIS_AGENT") == "1" and env.get("JAVIS_MERGE") != "1":
        return 1, "R4: workers do not commit; the lead uses `harness merge`"
    brain = env.get("JAVIS_BRAIN_DIR")
    ctx = GuardContext(repo=repo, home=Path.home(), brain_dir=Path(brain) if brain else None, worktree=None)
    for rel in filter(None, _git(repo, "diff", "--cached", "--name-only", "-z").split("\0")):
        verdict = check_path(repo / rel, "write", ctx)
        if not verdict.allowed:
            return 1, f"{verdict.rule}: {rel}: {verdict.reason}"
    added = "\n".join(
        line[1:]
        for line in _git(repo, "diff", "--cached", "-U0", "--no-color").splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    findings = scan_text(added)
    if findings:
        return 1, "R7: " + "; ".join(f.snippet for f in findings[:5])
    return 0, ""


def scan_all(repo: Path) -> list[str]:
    problems = []
    for rel in filter(None, _git(repo, "ls-files", "-z").split("\0")):
        try:
            text = (repo / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or vanished file
        problems += [f"{rel}:{f.line}: {f.snippet}" for f in scan_text(text)]
    return problems


def _cmd_claude_pre(args: argparse.Namespace) -> int:
    repo = repo_root()
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0  # not our protocol; never wedge the session on bad input
    code, message = claude_pre(payload, GuardContext.from_env(repo))
    if message:
        print(message, file=sys.stderr)
    return code


def _cmd_git_pre_commit(args: argparse.Namespace) -> int:
    code, message = git_pre_commit(repo_root(), os.environ)
    if message:
        print(f"blocked by harness: {message}", file=sys.stderr)
    return code


def _cmd_scan_all(args: argparse.Namespace) -> int:
    problems = scan_all(repo_root())
    for problem in problems:
        print(problem)
    return 1 if problems else 0


def register(subparsers) -> None:
    hook = subparsers.add_parser("hook", help="hook entry points (called by tools, not people)")
    sub = hook.add_subparsers(dest="hook", required=True)
    sub.add_parser("claude-pre").set_defaults(func=_cmd_claude_pre)
    sub.add_parser("git-pre-commit").set_defaults(func=_cmd_git_pre_commit)
    sub.add_parser("scan-all").set_defaults(func=_cmd_scan_all)
