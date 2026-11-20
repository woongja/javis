"""`harness dispatch <id> --role <role>`: run one worker in the task's worktree."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from harness import HarnessError, ledger, runner
from harness.paths import handoff_dir, repo_root, state_dir, worktree_dir
from harness.roles import load_roles
from harness.tasks import Task, bump_handoff, load, save

PROMPT = """You are a worker on the javis project. Follow AGENTS.md in this folder.

Task spec:
{spec}

Rules for this run:
- Work only inside this folder.
- Do NOT run git commit/push/merge. The lead merges after review.
- When done, write a short summary of what you changed and how you checked it to `.task/result.md`.
"""


def _ensure_worktree(root: Path, task: Task) -> Path:
    wt = worktree_dir(root, task.id)
    if not wt.exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "worktree", "add", "-q", "-b", task.branch, str(wt)],
                       cwd=root, check=True, capture_output=True, text=True)
    return wt


def dispatch(root: Path, task_id: str, role_name: str, timeout_s: int = 1800) -> Task:
    role = load_roles().get(role_name)
    if role is None:
        raise HarnessError(f"unknown role {role_name!r}")
    task = load(root, task_id)
    if task.status == "merged":
        raise HarnessError(f"{task_id} is already merged")
    task = bump_handoff(root, task_id)  # fails fast when the limit is reached
    wt = _ensure_worktree(root, task)
    spec = (handoff_dir(root, task_id) / "spec.md").read_text(encoding="utf-8")
    env = runner.safe_env(os.environ, {"JAVIS_AGENT": "1", "JAVIS_WORKTREE": str(wt), "JAVIS_TASK": task_id})
    cmd = runner.build_command(role, wt)
    log_path = state_dir(root) / "runs" / f"{task_id}-{task.handoffs}.log"
    meta = {"task": task_id, "role": role.name, "tool": role.tool, "model": role.model, "effort": role.effort}
    with runner.acquire_lock(root, role.tool):
        task.status = "running"
        save(root, task)
        ledger.append(root, "dispatch.start", **meta)
        result = runner.run(cmd, PROMPT.format(spec=spec), wt, env, timeout_s, log_path)
    produced = wt / ".task" / "result.md"
    if produced.exists():
        shutil.copyfile(produced, handoff_dir(root, task_id) / "result.md")
    if result.quota_hit:
        task.status = "blocked:quota"
    elif result.exit_code == 0 and not result.timed_out:
        task.status = "done"
    else:
        task.status = "failed"
    save(root, task)
    ledger.append(root, "dispatch.end", **meta, exit=result.exit_code, duration_s=result.duration_s,
                  status=task.status, log=str(log_path.relative_to(root)))
    return task


def _cmd_dispatch(args: argparse.Namespace) -> int:
    task = dispatch(repo_root(), args.task_id, args.role, args.timeout)
    print(f"{task.id}: {task.status}")
    return 0 if task.status == "done" else 1


def register(subparsers) -> None:
    p = subparsers.add_parser("dispatch", help="run a worker for a task in its worktree")
    p.add_argument("task_id")
    p.add_argument("--role", required=True)
    p.add_argument("--timeout", type=int, default=1800)
    p.set_defaults(func=_cmd_dispatch)
