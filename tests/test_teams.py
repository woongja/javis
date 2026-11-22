import tomllib
from pathlib import Path

from teams import Notice, WebAnswer, registry


def test_registry_lists_available_research_and_local_placeholders():
    class Engine:
        def run(self, text):
            assert text == "question"
            return response

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
        assert team.handler("private input", "cli") == Notice(reason)


def test_teams_is_configured_as_a_wheel_package():
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as source:
        config = tomllib.load(source)
    assert "teams" in config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert (root / "teams" / "__init__.py").is_file()
