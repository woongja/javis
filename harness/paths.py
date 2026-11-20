"""Well-known locations inside (and next to) the repository."""
from __future__ import annotations

from pathlib import Path

from harness import HarnessError


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
