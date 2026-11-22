import tomllib
from importlib.resources import files
from pathlib import Path


def test_hud_assets_are_in_wheel_package_tree():
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as source:
        config = tomllib.load(source)
    assert "hud" in config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    package = files("hud")
    assert package.joinpath("__init__.py").is_file()
    assert package.joinpath("index.html").is_file()
    for name in ("hud.css", "hud.js", "bg.js"):
        assert package.joinpath("static", name).is_file()
