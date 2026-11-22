import tomllib
from pathlib import Path

from teams import Notice, WebAnswer, registry


def test_registry_lists_research_and_unavailable_local_teams(monkeypatch):
    class Engine:
        def run(self, text):
            assert text == "question"
            return response

    monkeypatch.delenv("JAVIS_BRAIN_DIR", raising=False)
    monkeypatch.setattr("brain.env_value", lambda *args, **kwargs: None)
    response = object()
    teams = registry(Engine())
    assert {prefix: team.name for prefix, team in teams.items()} == {
        "/web": "research", "/brain": "brain", "/note": "note",
    }
    assert all(team.description for team in teams.values())
    assert teams["/web"].available() == (True, "")
    assert teams["/web"].handler("question", "cli") == WebAnswer(response)
    for prefix in ("/brain", "/note"):
        team = teams[prefix]
        available, reason = team.available()
        assert not available and reason
    assert teams["/brain"].handler("private input", "cli") == Notice(reason)


def test_teams_and_brain_are_configured_as_wheel_packages():
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as source:
        config = tomllib.load(source)
    packages = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    for name in ("teams", "brain"):
        assert name in packages
        assert (root / name / "__init__.py").is_file()
