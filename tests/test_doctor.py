import subprocess

import pytest

from harness.cli import main
from harness.doctor import _brain_isolation, parse_version, run_checks, select_lead
from tests.conftest import run


def _checks(root):
    return {c.name: c for c in run_checks(root)}


def test_parse_version():
    assert parse_version("codex-cli 0.164.0") == (0, 164, 0)
    assert parse_version("2.1.329 (Claude Code)") == (2, 1, 329)
    assert parse_version("garbage") == (0, 0, 0)


def test_codex_too_old(git_repo, fake_bins, monkeypatch):
    monkeypatch.setenv("JAVIS_FAKE_VERSION", "codex-cli 0.164.0")
    c = _checks(git_repo)["codex>=0.171.0"]
    assert c.status == "fail" and "npm i -g @openai/codex@latest" in c.fix


def test_codex_new_enough(git_repo, fake_bins, monkeypatch):
    monkeypatch.setenv("JAVIS_FAKE_VERSION", "codex-cli 0.171.0")
    assert _checks(git_repo)["codex>=0.171.0"].status == "ok"


def test_select_lead_fallback(git_repo, monkeypatch):
    monkeypatch.delenv("JAVIS_LEAD", raising=False)
    r = select_lead(git_repo, probe_fn=lambda role: role.tool == "codex")
    assert r.model == "gpt-6.2-astra"
    assert 'lead = "codex/gpt-6.2-astra/high"' in (git_repo / ".harness/local.toml").read_text(encoding="utf-8")


def test_select_lead_prefers_fable(git_repo, monkeypatch):
    monkeypatch.delenv("JAVIS_LEAD", raising=False)
    assert select_lead(git_repo, probe_fn=lambda role: True).model == "claude-fable-5-2"


def test_select_lead_forced(git_repo, monkeypatch):
    monkeypatch.setenv("JAVIS_LEAD", "codex")
    assert select_lead(git_repo, probe_fn=lambda role: True).tool == "codex"


def test_select_lead_none(git_repo, monkeypatch):
    monkeypatch.delenv("JAVIS_LEAD", raising=False)
    assert select_lead(git_repo, probe_fn=lambda role: False) is None


def test_env_keys_no_values(git_repo, fake_bins, monkeypatch, capsys):
    monkeypatch.delenv("JAVIS_BRAIN_DIR", raising=False)
    (git_repo / ".env.example").write_text("JAVIS_BRAIN_DIR=\n", encoding="utf-8")
    assert _checks(git_repo)["env-keys"].status == "warn"
    (git_repo / ".env").write_text("JAVIS_BRAIN_DIR=secretpath\n", encoding="utf-8")
    monkeypatch.chdir(git_repo)
    main(["doctor"])
    out = capsys.readouterr().out
    assert "secretpath" not in out and "env-keys" in out


@pytest.mark.parametrize(("configured", "exists", "status"), [
    (True, False, "fail"),
    (True, True, "ok"),
    (False, False, "warn"),
])
def test_brain_dir(git_repo, fake_bins, monkeypatch, capsys, configured, exists, status):
    monkeypatch.delenv("JAVIS_BRAIN_DIR", raising=False)
    monkeypatch.setattr("harness.platform.is_windows", lambda: False)
    brain_dir = git_repo / "example-brain-folder"
    if exists:
        brain_dir.mkdir()
    value = str(brain_dir) if configured else ""
    (git_repo / ".env").write_text(f"JAVIS_BRAIN_DIR={value}\n", encoding="utf-8")

    check = _checks(git_repo)["brain-dir"]
    assert check.status == status
    if status == "fail":
        assert check.detail == "JAVIS_BRAIN_DIR points to a missing folder"
        assert check.fix == "fix the path in .env"
    elif status == "warn":
        assert check.detail == "not set"

    monkeypatch.chdir(git_repo)
    main(["doctor"])
    output = capsys.readouterr()
    assert "brain-dir" in output.out
    assert brain_dir.name not in output.out + output.err


@pytest.mark.parametrize(("offline_ace", "online_ace", "online_exists", "status"), [
    ("(DENY)(OI)(CI)(R)", "(OI)(CI)(DENY)(RX)", True, "ok"),
    ("(OI)(CI)(R)", "(OI)(CI)(R)", True, "fail"),
    ("(DENY)(OI)(R)", "(DENY)(OI)(CI)(R)", True, "fail"),
    ("(DENY)(OI)(CI)(IO)(R)", "(DENY)(OI)(CI)(R)", True, "fail"),
    ("(DENY)(OI)(CI)(NP)(R)", "(DENY)(OI)(CI)(R)", True, "fail"),
    ("(DENY)(OI)(CI)(W)", "(DENY)(OI)(CI)(R)", True, "fail"),
    ("(DENY)(OI)(CI)(R)", "(OI)(CI)(R)", False, "ok"),
])
def test_windows_brain_isolation(git_repo, fake_bins, monkeypatch, capsys, offline_ace, online_ace,
                                 online_exists, status):
    brain = git_repo / "example private folder"
    brain.mkdir()
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(brain))
    monkeypatch.setattr("harness.platform.is_windows", lambda: True)
    native_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[0] == "powershell.exe":
            accounts = "EXAMPLE\\CodexSandboxOffline\tS-1-5-21-1000\n"
            if online_exists:
                accounts += "EXAMPLE\\CodexSandboxOnline\tS-1-5-21-1001\n"
            return subprocess.CompletedProcess(cmd, 0, accounts, "")
        if cmd[0] == "icacls":
            assert cmd == ["icacls", str(brain.resolve())]
            acl = f"{brain} EXAMPLE\\CodexSandboxOffline:{offline_ace}\n"
            acl += f"    *S-1-5-21-1001:{online_ace}\n"
            return subprocess.CompletedProcess(cmd, 0, acl, "")
        return native_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    check = _checks(git_repo)["brain-isolation"]
    assert check.status == status
    monkeypatch.chdir(git_repo)
    assert main(["doctor", "--fix"]) == (1 if status == "fail" else 0)
    output = capsys.readouterr()
    assert brain.name not in output.out + output.err
    if status == "fail":
        fix = 'icacls "%JAVIS_BRAIN_DIR%" /deny "CodexSandboxOffline:(OI)(CI)R"'
        if "DENY" not in online_ace:
            fix += ' "CodexSandboxOnline:(OI)(CI)R"'
        assert check.fix == fix
        assert fix in output.out


def test_brain_isolation_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("JAVIS_BRAIN_DIR", raising=False)
    check = _brain_isolation(tmp_path)
    assert (check.status, check.detail) == ("warn", "not set")


def test_brain_isolation_non_windows(tmp_path, monkeypatch):
    monkeypatch.setenv("JAVIS_BRAIN_DIR", "example brain")
    monkeypatch.setattr("harness.platform.is_windows", lambda: False)
    check = _brain_isolation(tmp_path)
    assert check.status == "ok"
    assert check.detail == "claude sandbox denyRead applied at dispatch (codex: unverified)"


def test_fix_sets_hooks_path(git_repo, fake_bins, monkeypatch):
    monkeypatch.chdir(git_repo)
    assert _checks(git_repo)["hooksPath"].status == "fail"
    main(["doctor", "--fix"])
    assert run(["git", "config", "--get", "core.hooksPath"], git_repo).strip() == ".githooks"
