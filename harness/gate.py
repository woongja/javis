"""`harness gate <id>`: machine-check a task's completion criteria."""
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
from harness.runner import safe_env
from harness.tasks import load, save

_BLOCK = re.compile(r"```gate[ \t]*\n(.*?)```", re.S)


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


def _git(wt: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=wt, check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


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


def run_gate(root: Path, task_id: str, timeout_s: int = 600) -> GateResult:
    task = load(root, task_id)
    wt = worktree_dir(root, task_id)
    if not wt.exists():
        raise HarnessError(f"{task_id} has no worktree; dispatch it first")
    # Invalidate any earlier approval first, so a crash mid-gate never leaves a stale pass.
    task.status, task.gate_tree = ("done" if task.status == "gated" else task.status), None
    save(root, task)
    commands = parse_gate_commands((handoff_dir(root, task_id) / "spec.md").read_text(encoding="utf-8"))
    env = safe_env(os.environ, {})
    results = [_run_one(argv, wt, env, timeout_s) for argv in commands]
    r7 = _r7_check(wt)
    if r7:
        results.append(r7)
    tree = _git(wt, "write-tree").strip()
    passed = bool(commands) and all(r["exit"] == 0 for r in results)
    result = GateResult(passed, results, tree)
    (handoff_dir(root, task_id) / "gate.json").write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2),
                                                          encoding="utf-8")
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
        print(f"{'✅' if r['exit'] == 0 else '✗'} exit={r['exit']:<4} {r['duration_s']:>6}s  {r['cmd']}")
    print("PASSED" if result.passed else "FAILED")
    return 0 if result.passed else 1


def register(subparsers) -> None:
    p = subparsers.add_parser("gate", help="run a task's completion checks")
    p.add_argument("task_id")
    p.add_argument("--timeout", type=int, default=600)
    p.set_defaults(func=_cmd_gate)
