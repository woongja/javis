"""Hook entry points: Claude and Codex PreToolUse, git pre-commit, CI scan."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from harness.guards import GuardContext, added_lines, check_command, check_command_paths, check_path, scan_text
from harness.paths import env_path, repo_root

_FILE_TOOLS = {"Read": "read", "Write": "write", "Edit": "write", "MultiEdit": "write", "NotebookEdit": "write"}
_PATH_FIELDS = ("file_path", "notebook_path")
_SEARCH_TOOLS = {"Grep", "Glob"}
_SHELL_TOOLS = {"Bash", "PowerShell"}
_PATCH_HEADERS = ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: ")


def claude_pre(payload: dict, ctx: GuardContext) -> tuple[int, str]:
    tool = payload.get("tool_name", "")
    data = payload.get("tool_input") or {}
    verdict = None
    target = next((data[f] for f in _PATH_FIELDS if data.get(f)), None)
    if tool in _FILE_TOOLS and target:
        verdict = check_path(Path(target), _FILE_TOOLS[tool], ctx)
    elif tool in _SHELL_TOOLS and data.get("command"):
        verdict = check_command(data["command"], ctx)
        if verdict.allowed:
            verdict = check_command_paths(data["command"], ctx, Path(payload.get("cwd") or ctx.worktree or ctx.repo))
    elif tool in _SEARCH_TOOLS and data.get("path"):
        verdict = check_path(Path(data["path"]), "read", ctx)
    if verdict is None or verdict.allowed:
        return 0, ""
    return 2, f"{verdict.rule}: {verdict.reason}"


def codex_pre(payload: dict, ctx: GuardContext) -> tuple[bool, str]:
    if not isinstance(payload, dict) or not isinstance(tool := payload.get("tool_name"), str):
        raise ValueError("expected a hook payload with a tool name")
    if tool not in {"Bash", "apply_patch"}:
        return True, ""
    data = payload.get("tool_input")
    if not isinstance(data, dict) or not isinstance(command := data.get("command"), str):
        raise ValueError("expected a tool input with a command string")
    cwd = Path(payload.get("cwd", ctx.worktree or ctx.repo))
    if tool == "Bash":
        verdict = check_command(command, ctx)
        if verdict.allowed:
            verdict = check_command_paths(command, ctx, cwd)
        if not verdict.allowed:
            return False, f"{verdict.rule}: {verdict.reason}"
    else:
        for line in command.splitlines():
            if line.startswith(_PATCH_HEADERS):
                verdict = check_path(cwd / line.split(": ", 1)[1].strip(), "write", ctx)
                if not verdict.allowed:
                    return False, f"{verdict.rule}: {verdict.reason}"
    return True, ""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout


def git_pre_commit(repo: Path, env: Mapping[str, str]) -> tuple[int, str]:
    if env.get("JAVIS_AGENT") == "1" and env.get("JAVIS_MERGE") != "1":
        return 1, "R4: workers do not commit; the lead uses `harness merge`"
    brain = env_path(repo, "JAVIS_BRAIN_DIR", env=env)
    ctx = GuardContext(repo=repo, home=Path.home(), brain_dir=brain, worktree=None)
    for rel in filter(None, _git(repo, "diff", "--cached", "--name-only", "-z").split("\0")):
        verdict = check_path(repo / rel, "write", ctx)
        if not verdict.allowed:
            return 1, f"{verdict.rule}: {rel}: {verdict.reason}"
    findings = scan_text("\n".join(added_lines(_git(repo, "diff", "--cached", "-U0", "--no-color"))))
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


def scan_history(repo: Path) -> list[str]:
    history = _git(
        repo, "-c", "core.quotePath=false", "log", "--all", "-p", "-U0", "--no-color",
        "--format=commit %h", "--no-notes", "--no-show-signature", "--no-ext-diff",
        "--no-textconv", "--no-relative", "--src-prefix=a/", "--dst-prefix=b/",
    )
    problems = []
    for commit_patch in ("\n" + history).split("\ncommit ")[1:]:
        header, *file_patches = commit_patch.split("\ndiff --git ")
        commit = header.partition("\n")[0]
        for file_patch in file_patches:
            findings = scan_text("\n".join(added_lines(file_patch)))
            if findings:
                # The destination header keeps the entire path, including spaces.
                _, _, destination = file_patch.partition("\n+++ ")
                path = destination.partition("\n")[0].removesuffix("\t")
                if path.startswith('"b/'):
                    path = '"' + path[3:]
                else:
                    path = path.removeprefix("b/")
                problems += [f"{commit} {path}: {finding.snippet}" for finding in findings]
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


def _cmd_codex_pre(args: argparse.Namespace) -> int:
    try:
        try:
            payload = json.load(getattr(sys.stdin, "buffer", sys.stdin))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return 0
        worktree = Path(value) if (value := os.environ.get("JAVIS_WORKTREE")) else None
        brain = os.environ.get("JAVIS_BRAIN_DIR")
        ctx = GuardContext(args.repo or repo_root(), Path.home(), Path(brain) if brain else None, worktree)
        allowed, message = codex_pre(payload, ctx)
    except Exception:
        allowed, message = False, "R1: hook error, fail closed"
    if not allowed:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                               "permissionDecision": "deny",
                                               "permissionDecisionReason": message}}))
    return 0


def _cmd_scan_all(args: argparse.Namespace) -> int:
    repo = repo_root()
    problems = scan_history(repo) if args.history else scan_all(repo)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


def register(subparsers) -> None:
    hook = subparsers.add_parser("hook", help="hook entry points (called by tools, not people)")
    sub = hook.add_subparsers(dest="hook", required=True)
    sub.add_parser("claude-pre").set_defaults(func=_cmd_claude_pre)
    codex = sub.add_parser("codex-pre")
    codex.add_argument("--repo", type=Path, help="lead repository root")
    codex.set_defaults(func=_cmd_codex_pre)
    sub.add_parser("git-pre-commit").set_defaults(func=_cmd_git_pre_commit)
    scan = sub.add_parser("scan-all")
    scan.add_argument("--history", action="store_true", help="scan added lines across all Git refs")
    scan.set_defaults(func=_cmd_scan_all)
