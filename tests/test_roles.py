import pytest

from harness import HarnessError
from harness.roles import Role, lead_candidates, load_roles, owner_of


def test_role_values():
    r = load_roles()
    assert r["codex.reviewer"] == Role("codex.reviewer", "codex", "gpt-6.2-sol", "high")
    assert r["claude.simple"].model == "claude-sonnet-5-5" and r["claude.simple"].effort == "low"
    assert r["claude.dev_hard"] == Role("claude.dev_hard", "claude", "claude-opus-5-5", "high")
    assert r["codex.dev_hard"] == Role("codex.dev_hard", "codex", "gpt-6.2-astra", "high")
    assert r["codex.visual"].effort == "low"
    assert len(r) == 8


def test_lead_order():
    leads = lead_candidates()
    assert [c.model for c in leads] == ["claude-fable-5-2", "gpt-6.2-astra"]
    assert all(c.name == "lead" and c.effort == "high" for c in leads)


def test_invalid_effort_rejected(tmp_path):
    p = tmp_path / "r.toml"
    p.write_text('[codex.dev]\ntool="codex"\nmodel="x"\neffort="ultra"\n', encoding="utf-8")
    with pytest.raises(HarnessError, match="effort"):
        load_roles(p)


def test_invalid_tool_rejected(tmp_path):
    p = tmp_path / "r.toml"
    p.write_text('[x.dev]\ntool="gemini"\nmodel="x"\neffort="low"\n', encoding="utf-8")
    with pytest.raises(HarnessError, match="tool"):
        load_roles(p)


def test_owner_of():
    assert owner_of("hud/scene.ts") == "codex" and owner_of("brain/write.py") == "claude"
    assert owner_of("channels/telegram/bot.py") == "claude"
    assert owner_of("README.md") is None
