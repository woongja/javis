from harness.paths import worktree_dir
from harness.platform import find_exe


def test_find_exe_override(tmp_path, monkeypatch):
    fake = tmp_path / "fake tool.py"
    fake.write_text("")
    monkeypatch.setenv("JAVIS_CLAUDE_BIN", str(fake))
    assert find_exe("claude", "JAVIS_CLAUDE_BIN") == fake


def test_find_exe_uses_which(tmp_path, monkeypatch):
    shim = tmp_path / "npm" / "codex.CMD"
    monkeypatch.delenv("JAVIS_CODEX_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda n: str(shim))
    found = find_exe("codex", "JAVIS_CODEX_BIN")
    assert found == shim
    assert found.suffix == ".CMD"


def test_find_exe_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n: None)
    assert find_exe("nope") is None


def test_worktree_dir_outside_repo(git_repo):
    wt = worktree_dir(git_repo, "T001")
    assert wt.parent.name == f"{git_repo.name}-wt" and git_repo not in wt.parents
