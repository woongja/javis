"""Task state (.harness/tasks/<id>.json) and `harness task new`."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from harness import HarnessError, ledger
from harness.paths import handoff_dir, repo_root, state_dir

HANDOFF_LIMIT = 4
OWNERS = ("claude", "codex")
STATUSES = ("new", "running", "done", "blocked:quota", "failed", "gated", "merged")
_TEMPLATE = Path(__file__).with_name("templates") / "spec.md"


@dataclass
class Task:
    id: str
    title: str
    owner: str
    status: str
    handoffs: int
    branch: str
    gate_tree: str | None = None


def _tasks_dir(root: Path) -> Path:
    return state_dir(root) / "tasks"


def save(root: Path, task: Task) -> None:
    folder = _tasks_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(asdict(task), fh, ensure_ascii=False, indent=2)
    os.replace(tmp, folder / f"{task.id}.json")


def load(root: Path, task_id: str) -> Task:
    path = _tasks_dir(root) / f"{task_id}.json"
    if not path.exists():
        raise HarnessError(f"unknown task {task_id}")
    return Task(**json.loads(path.read_text(encoding="utf-8")))


def _next_id(root: Path) -> str:
    folder = _tasks_dir(root)
    numbers = [int(p.stem[1:]) for p in folder.glob("T*.json") if p.stem[1:].isdigit()] if folder.exists() else []
    return f"T{max(numbers, default=0) + 1:03d}"


def new_task(root: Path, title: str, owner: str) -> Task:
    if owner not in OWNERS:
        raise HarnessError(f"owner must be one of {OWNERS}, got {owner!r}")
    task_id = _next_id(root)
    task = Task(id=task_id, title=title, owner=owner, status="new", handoffs=0, branch=f"task/{task_id}")
    folder = handoff_dir(root, task_id)
    folder.mkdir(parents=True, exist_ok=True)
    spec = _TEMPLATE.read_text(encoding="utf-8").format(task_id=task_id, title=title, owner=owner)
    (folder / "spec.md").write_text(spec, encoding="utf-8")
    save(root, task)
    ledger.append(root, "task.new", task=task_id, title=title, owner=owner)
    return task


def bump_handoff(root: Path, task_id: str) -> Task:
    task = load(root, task_id)
    if task.handoffs >= HANDOFF_LIMIT:
        raise HarnessError(f"{task_id}: handoff limit {HANDOFF_LIMIT} reached; lead must re-plan")
    task.handoffs += 1
    save(root, task)
    return task


def _cmd_new(args: argparse.Namespace) -> int:
    print(new_task(repo_root(), args.title, args.owner).id)
    return 0


def register(subparsers) -> None:
    task = subparsers.add_parser("task", help="create and inspect tasks")
    sub = task.add_subparsers(dest="task_cmd", required=True)
    new = sub.add_parser("new", help="create a task and its spec template")
    new.add_argument("title")
    new.add_argument("--owner", choices=OWNERS, required=True)
    new.set_defaults(func=_cmd_new)
