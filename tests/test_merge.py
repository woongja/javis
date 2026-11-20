import os
import sys
from pathlib import Path

import pytest

from harness import HarnessError
from harness.merge import merge
from harness.paths import worktree_dir
from harness.tasks import load
from tests.conftest import run

HARNESS_SRC = Path(__file__).resolve().parents[1]


def test_merge_happy(gated_task):
    root, task_id = gated_task
    sha = merge(root, task_id)
    assert run(["git", "log", "-1", "--format=%s"], root).strip() == f"feat(codex)({task_id}): demo"
    assert run(["git", "rev-parse", "HEAD"], root).strip() == sha
    assert (root / "hud" / "demo.txt").exists()
    assert not worktree_dir(root, task_id).exists()
    assert "task/" not in run(["git", "branch"], root)
    assert load(root, task_id).status == "merged"


def test_merge_rejects_ungated(dispatched_task):
    with pytest.raises(HarnessError, match="not gated"):
        merge(*dispatched_task)


def test_merge_rejects_changed_after_gate(gated_task):
    (worktree_dir(*gated_task) / "hud" / "new.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(HarnessError, match="changed since gate"):
        merge(*gated_task)


def test_merge_rejects_dirty_main(gated_task):
    (gated_task[0] / "stray.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(HarnessError, match="main not clean"):
        merge(*gated_task)


def test_merge_archives_handoff(gated_task, tmp_path, monkeypatch):
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(tmp_path / "brain"))
    merge(*gated_task)
    assert (tmp_path / "brain" / "javis-dev" / "handoff" / gated_task[1] / "spec.md").exists()


def test_pre_commit_hook_allows_merge(gated_task, monkeypatch):
    root, task_id = gated_task
    hook = root / ".githooks" / "pre-commit"
    hook.parent.mkdir()
    hook.write_text(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" -m harness.cli hook git-pre-commit\n',
                    encoding="utf-8")
    hook.chmod(0o755)
    run(["git", "add", ".githooks"], root)
    run(["git", "commit", "-q", "-m", "hooks"], root)
    run(["git", "config", "core.hooksPath", ".githooks"], root)
    monkeypatch.setenv("PYTHONPATH", str(HARNESS_SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    merge(root, task_id)
    assert load(root, task_id).status == "merged"
