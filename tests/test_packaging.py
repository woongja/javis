import os
import shutil
import subprocess
import sys
import tomllib
import venv
from importlib.resources import files
from pathlib import Path
from zipfile import ZipFile

import pytest

from harness.runner import safe_env

ROOT = Path(__file__).resolve().parents[1]
HUD_STATIC = ("hud.css", "hud.js", "bg.js")


def test_hud_assets_are_in_wheel_package_tree():
    with (ROOT / "pyproject.toml").open("rb") as source:
        config = tomllib.load(source)
    assert "hud" in config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    package = files("hud")
    assert package.joinpath("__init__.py").is_file()
    assert package.joinpath("index.html").is_file()
    for name in HUD_STATIC:
        assert package.joinpath("static", name).is_file()


@pytest.fixture(scope="module")
def wheel_build(tmp_path_factory):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build and install the wheel")

    directory = tmp_path_factory.mktemp("packaging")
    extra_env = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    if cache := os.environ.get("UV_CACHE_DIR"):
        extra_env["UV_CACHE_DIR"] = str(Path(cache).resolve())
    if find_links := os.environ.get("UV_FIND_LINKS"):
        extra_env["UV_FIND_LINKS"] = find_links
    env = safe_env(os.environ, extra_env)

    def run(*args):
        return subprocess.run(
            args, cwd=directory, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )

    uv_offline = (uv, "--offline", "--no-python-downloads")
    result = run(*uv_offline, "build", "--wheel", "--python", sys.executable,
                 "--out-dir", str(directory), str(ROOT))
    if result.returncode and "Packages were unavailable because the network was disabled" in result.stderr:
        pytest.skip("wheel build requires build-system packages unavailable in the offline uv cache")
    assert result.returncode == 0, result.stderr
    wheel, = directory.glob("*.whl")
    env["JAVIS_DATA_DIR"] = str(directory / "data")
    return wheel, uv_offline, run


def test_wheel_contents(wheel_build):
    wheel, _, _ = wheel_build
    with ZipFile(wheel) as archive:
        members = set(archive.namelist())
    for package in ("harness", "core", "worklog", "channels", "teams", "brain", "hud"):
        assert f"{package}/__init__.py" in members
    for name in members:
        parts = name.lower().split("/")
        assert not {"tests", ".handoff"}.intersection(parts), name
        assert not any(part.startswith((".env", "test_")) for part in parts), name


def test_installed_wheel(wheel_build):
    wheel, uv_offline, run = wheel_build
    environment = wheel.parent / "venv"
    venv.EnvBuilder().create(environment)
    if os.name == "nt":
        python = environment / "Scripts" / "python.exe"
        javis = environment / "Scripts" / "javis.exe"
    else:
        python = environment / "bin" / "python"
        javis = environment / "bin" / "javis"
    result = run(*uv_offline, "pip", "install", "--python", str(python), "--no-deps", str(wheel))
    assert result.returncode == 0, result.stderr

    for args in (("--help",), ("search", "--help"), ("brain", "import", "--help"), ("hud", "--help")):
        result = run(str(javis), *args)
        assert result.returncode == 0, (args, result.stderr)
        assert "usage:" in result.stdout

    result = run(str(javis), "voice", "status")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert "enabled=false" in lines
    for direction in ("tts", "stt"):
        assert any(line.startswith(f"{direction}=false ") and line.endswith("available=no (disabled)")
                   for line in lines), result.stdout

    result = run(str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))")
    assert result.returncode == 0, result.stderr
    installed = Path(result.stdout.strip())
    assert installed.is_relative_to(environment)
    assert (installed / "hud" / "index.html").is_file()
    for name in HUD_STATIC:
        assert (installed / "hud" / "static" / name).is_file()
    with (installed / "core" / "settings.toml").open("rb") as source:
        voice = tomllib.load(source)["voice"]
    for switch in ("enabled", "tts", "stt"):
        assert voice[switch] is False
