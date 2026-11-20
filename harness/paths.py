"""Well-known locations inside (and next to) the repository."""
from __future__ import annotations

import os
from pathlib import Path

from harness import HarnessError


def env_value(root: Path, key: str) -> str | None:
    """Look up a setting in the process environment, then the repo's .env."""
    if value := os.environ.get(key):
        return value
    try:
        lines = (root / ".env").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    for line in lines:
        if line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if separator and name.strip() == key:
            value = value.strip()
            if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                value = value[1:-1]
            return value.strip()
    return None


def repo_root(start: Path | None = None) -> Path:
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    raise HarnessError(f"not inside a git repository: {here}")


def state_dir(root: Path) -> Path:
    return root / ".harness"


def handoff_dir(root: Path, task_id: str) -> Path:
    return root / ".handoff" / task_id


def worktree_dir(root: Path, task_id: str) -> Path:
    # Per-repo folder so two clones side by side never share a T001 worktree.
    return root.parent / f"{root.name}-wt" / task_id
