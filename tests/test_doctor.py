import os

import pytest

from harness.cli import main
from harness.doctor import parse_version, run_checks, select_lead
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


def test_fix_sets_hooks_path(git_repo, fake_bins, monkeypatch):
    monkeypatch.chdir(git_repo)
    assert _checks(git_repo)["hooksPath"].status == "fail"
    main(["doctor", "--fix"])
    assert run(["git", "config", "--get", "core.hooksPath"], git_repo).strip() == ".githooks"
