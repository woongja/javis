"""`harness merge <id>`: the only path that commits to main (lead only)."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from harness import HarnessError, ledger
from harness.paths import handoff_dir, repo_root, worktree_dir
from harness.tasks import load, save

BASE_BRANCH = "main"


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise HarnessError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def _check_preconditions(root: Path, task_id: str) -> Path:
    task = load(root, task_id)
    if task.status != "gated" or not task.gate_tree:
        raise HarnessError(f"{task_id} is not gated (status: {task.status}); run `harness gate {task_id}`")
    wt = worktree_dir(root, task_id)
    _git(wt, "add", "-A")
    if _git(wt, "write-tree").strip() != task.gate_tree:
        raise HarnessError(f"{task_id} changed since gate; run `harness gate {task_id}` again")
    if _git(root, "symbolic-ref", "--short", "HEAD").strip() != BASE_BRANCH:
        raise HarnessError(f"main not checked out in {root}")
    if _git(root, "status", "--porcelain").strip():
        raise HarnessError("main not clean; commit or stash your own changes first")
    return wt


def merge(root: Path, task_id: str) -> str:
    wt = _check_preconditions(root, task_id)
    task = load(root, task_id)
    env = {k: v for k, v in os.environ.items() if k.upper() != "JAVIS_AGENT"}
    env["JAVIS_MERGE"] = "1"
    # Commit exactly the tree the gate approved (not whatever the index holds now).
    parent = _git(wt, "rev-parse", "HEAD").strip()
    wip = _git(wt, "commit-tree", task.gate_tree, "-p", parent, "-m", f"wip({task_id})", env=env).strip()
    _git(root, "update-ref", f"refs/heads/{task.branch}", wip, parent)
    _git(root, "merge", "--squash", "--no-overwrite-ignore", task.branch, env=env)
    _git(root, "commit", "-q", "--allow-empty", "-m", f"feat({task.owner})({task_id}): {task.title}", env=env)
    sha = _git(root, "rev-parse", "HEAD").strip()
    task.status = "merged"  # recorded before cleanup so a later failure never hides the merge
    save(root, task)
    ledger.append(root, "merge", task=task_id, sha=sha)
    _git(root, "worktree", "remove", "--force", str(wt))
    _git(root, "branch", "-D", task.branch)
    brain = os.environ.get("JAVIS_BRAIN_DIR")
    if brain:
        try:
            shutil.copytree(handoff_dir(root, task_id), Path(brain) / "javis-dev" / "handoff" / task_id,
                            symlinks=True, dirs_exist_ok=True)
        except OSError as exc:
            raise HarnessError(f"merged as {sha[:7]}, but archive to brain failed: {exc}") from exc
    return sha


def _cmd_merge(args: argparse.Namespace) -> int:
    print(merge(repo_root(), args.task_id))
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("merge", help="squash-merge a gated task into main (lead only)")
    p.add_argument("task_id")
    p.set_defaults(func=_cmd_merge)
