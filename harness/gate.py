"""`harness gate <id>`: machine-check a task's completion criteria.

R3  staged changes must stay within the task's allowed paths.
    why: out-of-spec changes can slip in without review. expires: never.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from harness import HarnessError, ledger, platform
from harness.guards import added_lines, scan_text
from harness.paths import handoff_dir, repo_root, worktree_dir
from harness.review import opposite_team, verdict
from harness.roles import load_ownership
from harness.runner import git as _git, safe_env
from harness.tasks import load, save

_BLOCK = re.compile(r"```gate[ \t]*\n(.*?)```", re.S)
_SCOPE = re.compile(r"^##[ \t]*건드릴 경로[^\n]*\n(.*?)(?=^## |\Z)", re.M | re.S)
_PATH_TOKEN = re.compile(r"(?<!`)(`+)([^`\n]+)\1(?!`)")
_DIFF_SCOPE = ("--no-renames", "--ignore-submodules=none", "--no-relative", "-z")
_REVIEW_PREFIXES = ("harness/", "security/", ".githooks/", ".github/", ".claude/")


@dataclass
class GateResult:
    passed: bool
    results: list[dict]
    tree: str


def parse_gate_commands(spec_text: str) -> list[list[str]]:
    match = _BLOCK.search(spec_text.replace("\r\n", "\n"))
    if not match:
        return []
    lines = (line.strip() for line in match.group(1).splitlines())
    return [_split(line) for line in lines if line and not line.startswith("#")]


def _split(line: str) -> list[str]:
    if not platform.is_windows():
        return shlex.split(line, posix=True)
    # POSIX rules eat backslashes in C:\paths; Windows rules keep them but also keep quotes.
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t
            for t in shlex.split(line, posix=False)]


def _run_one(argv: list[str], wt: Path, env: dict, timeout_s: int) -> dict:
    exe = shutil.which(argv[0], path=env.get("PATH")) or argv[0]
    started = time.monotonic()
    try:
        code = subprocess.run([exe, *argv[1:]], cwd=wt, env=env, capture_output=True, timeout=timeout_s).returncode
    except FileNotFoundError:
        code = 127
    except subprocess.TimeoutExpired:
        code = 124
    return {"cmd": shlex.join(argv), "exit": code, "duration_s": round(time.monotonic() - started, 2)}


def _r7_check(wt: Path) -> dict | None:
    _git(wt, "add", "-A")  # stage only; workers never commit
    findings = scan_text("\n".join(added_lines(_git(wt, "diff", "--cached", "-U0", "--no-color", "HEAD"))))
    if not findings:
        return None
    return {"cmd": "R7 scan: " + "; ".join(f.snippet for f in findings[:5]), "exit": 1, "duration_s": 0}


def _r3_check(wt: Path, spec: str, owner: str) -> dict | None:
    _git(wt, "add", "-A")

    def normalize(path: str) -> str:
        return path.lower() if platform.is_windows() else path

    section = _SCOPE.search(spec)
    tokens = _PATH_TOKEN.findall(section.group(1)) if section else []
    paths = [normalize(token.replace("\\", "/")) for _, token in tokens]
    files = {path for path in paths if not path.endswith("/")}
    prefixes = tuple(path for path in paths if path.endswith("/"))
    if not paths:
        prefixes = tuple(normalize(path.replace("\\", "/")) for path in load_ownership().get(owner, []) if path)

    # Disabling rename detection lists the source deletion and destination addition separately.
    changed = _git(wt, "diff", "--cached", "--name-only", *_DIFF_SCOPE, "HEAD")
    outside = []
    for path in changed.split("\0"):
        normalized = normalize(path)
        if path and normalized not in files and not normalized.startswith(prefixes):
            outside.append(path)
    if outside:
        return {"cmd": "R3 scope: " + "; ".join(outside[:5]), "exit": 1, "duration_s": 0}
    return None


def _requires_review(wt: Path, tree: str) -> bool:
    stats = _git(wt, "diff", "--numstat", "--no-ext-diff", "--no-textconv", *_DIFF_SCOPE, "HEAD", tree)
    total = 0
    windows = platform.is_windows()
    for entry in filter(None, stats.split("\0")):
        added, deleted, path = entry.split("\t", 2)
        if windows:
            path = path.lower()
        if path == ("agents.md" if windows else "AGENTS.md") or path.startswith(_REVIEW_PREFIXES):
            return True
        total += sum(int(count) for count in (added, deleted) if count != "-")
    return total >= 50


def _review_passed(path: Path, tree: str, owner: str) -> bool:
    try:
        header, _, output = path.read_text(encoding="utf-8").partition("\n")
    except (OSError, UnicodeError):
        return False
    role_header, _, output = output.partition("\n")
    prefix = f"role: {opposite_team(owner)}."
    return (header == f"tree: {tree}" and role_header.startswith(prefix)
            and bool(role_header[len(prefix):].strip()) and verdict(output) == "pass")


def run_gate(root: Path, task_id: str, timeout_s: int = 600) -> GateResult:
    task = load(root, task_id)
    wt = worktree_dir(root, task_id)
    if not wt.exists():
        raise HarnessError(f"{task_id} has no worktree; dispatch it first")
    # Invalidate any earlier approval first, so a crash mid-gate never leaves a stale pass.
    task.status, task.gate_tree = ("done" if task.status == "gated" else task.status), None
    save(root, task)
    folder = handoff_dir(root, task_id)
    spec = (folder / "spec.md").read_text(encoding="utf-8")
    commands = parse_gate_commands(spec)
    env = safe_env(os.environ, {})
    results = [_run_one(argv, wt, env, timeout_s) for argv in commands]
    r7 = _r7_check(wt)
    if r7:
        results.append(r7)
    r3 = _r3_check(wt, spec, task.owner)
    if r3:
        results.append(r3)
    tree = _git(wt, "write-tree").strip()
    if _requires_review(wt, tree) and not _review_passed(folder / "review.md", tree, task.owner):
        results.append({"cmd": f"review required: harness review {task_id}", "exit": 1})
    passed = bool(commands) and all(r["exit"] == 0 for r in results)
    result = GateResult(passed, results, tree)
    (folder / "gate.json").write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
    if passed:
        task.status, task.gate_tree = "gated", tree
        save(root, task)
    ledger.append(root, "gate", task=task_id, passed=passed, commands=len(commands))
    return result


def _cmd_gate(args: argparse.Namespace) -> int:
    result = run_gate(repo_root(), args.task_id, args.timeout)
    if not result.results:
        print("no gate commands: add lines to the ```gate block in spec.md")
    for r in result.results:
        print(f"{'✅' if r['exit'] == 0 else '✗'} exit={r['exit']:<4} {r.get('duration_s', 0):>6}s  {r['cmd']}")
    print("PASSED" if result.passed else "FAILED")
    return 0 if result.passed else 1


def register(subparsers) -> None:
    p = subparsers.add_parser("gate", help="run a task's completion checks")
    p.add_argument("task_id")
    p.add_argument("--timeout", type=int, default=600)
    p.set_defaults(func=_cmd_gate)
