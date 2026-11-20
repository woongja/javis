"""Regression tests for the Codex cross-review findings (2026-11-20)."""
import os
import sys

import pytest

from harness import HarnessError
from harness.dispatch import dispatch
from harness.gate import parse_gate_commands, run_gate
from harness.guards import GuardContext, added_lines, scan_text
from harness.hooks import claude_pre
from harness.merge import merge
from harness.paths import handoff_dir, worktree_dir
from harness.runner import acquire_lock, safe_env
from harness.tasks import load, new_task
from tests.conftest import HUD_EXISTS, set_gate


def test_notebook_edit_uses_notebook_path(git_repo, tmp_path):
    ctx = GuardContext(repo=git_repo, home=tmp_path, brain_dir=None, worktree=None)
    payload = {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": str(git_repo / ".env")}}
    assert claude_pre(payload, ctx)[0] == 2


def test_scan_checks_every_match_on_a_line():
    assert len(scan_text("ok@example.com then real@acme.io")) == 1  # harness:allow


def test_added_lines_keeps_content_starting_with_plus():
    diff = ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1,2 @@\n"
            "++token ghp_" + "d" * 36 + "\n+normal\n")
    assert added_lines(diff) == ["+token ghp_" + "d" * 36, "normal"]


def test_safe_env_drops_unknown_javis_vars():
    env = safe_env({"JAVIS_SERVICE_API_KEY": "s", "JAVIS_TASK": "T1", "PATH": "p"}, {})
    assert "JAVIS_SERVICE_API_KEY" not in env and env["JAVIS_TASK"] == "T1"


def test_busy_dispatch_does_not_consume_handoff(git_repo_with_harness, fake_bins):
    root = git_repo_with_harness
    task = new_task(root, "x", "codex")
    with acquire_lock(root, "codex"):
        with pytest.raises(HarnessError, match="busy"):
            dispatch(root, task.id, "codex.dev")
    assert load(root, task.id).handoffs == 0


def test_gate_rerun_invalidates_previous_approval(gated_task, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("crash mid-gate")

    monkeypatch.setattr("harness.gate._run_one", boom)
    with pytest.raises(RuntimeError):
        run_gate(*gated_task)
    task = load(*gated_task)
    assert task.status != "gated" and task.gate_tree is None


def test_merge_records_merged_before_archive(gated_task, monkeypatch, tmp_path):
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(tmp_path / "brain"))

    def fail_copy(*a, **k):
        assert load(*gated_task).status == "merged"
        raise OSError("disk full")

    monkeypatch.setattr("harness.merge.shutil.copytree", fail_copy)
    _, warnings = merge(*gated_task)
    assert warnings == ["archive to brain failed: disk full"]
    assert load(*gated_task).status == "merged"


def test_merge_commits_exactly_the_gated_tree(gated_task):
    root, task_id = gated_task
    merge(root, task_id)
    assert (root / "hud" / "demo.txt").read_text(encoding="utf-8") == "demo\n"


def test_result_symlink_not_followed(git_repo_with_harness, fake_bins, monkeypatch, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    probe = tmp_path / "probe-link"
    try:
        probe.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks need extra privileges on this OS")
    monkeypatch.setenv("JAVIS_FAKE_MODE", "symlink")
    monkeypatch.setenv("JAVIS_FAKE_LINK_TARGET", str(secret))
    root = git_repo_with_harness
    task = dispatch(root, new_task(root, "x", "codex").id, "codex.dev")
    copied = handoff_dir(root, task.id) / "result.md"
    assert not copied.exists() or "SECRET" not in copied.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path parsing")
def test_gate_parses_unquoted_windows_path():
    spec = "```gate\r\nC:\\Tools\\py.exe -c x\r\n```\r\n"
    assert parse_gate_commands(spec) == [["C:\\Tools\\py.exe", "-c", "x"]]


def test_gate_parses_quoted_args_everywhere():
    spec = '```gate\npython -c "print(1)"\n```\n'
    assert parse_gate_commands(spec) == [["python", "-c", "print(1)"]]


def test_worktree_dir_is_per_repo(git_repo):
    assert worktree_dir(git_repo, "T001").parent.name == f"{git_repo.name}-wt"


def test_task_id_format_validated(git_repo):
    with pytest.raises(HarnessError, match="task id"):
        load(git_repo, "../evil")
