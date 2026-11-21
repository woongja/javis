"""Runtime configuration for the local gateway."""

import math
import os
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path


@dataclass(frozen=True)
class EngineSettings:
    model: str | None = None
    timeout_s: float = 300


def load_settings() -> EngineSettings:
    with files("core").joinpath("settings.toml").open("rb") as source:
        engine = tomllib.load(source).get("engine", {})
    if not isinstance(engine, dict):
        raise ValueError("invalid engine settings")
    model = engine.get("model")
    timeout = engine.get("timeout_s", 300)
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("invalid engine model")
    if type(timeout) not in (int, float) or not 0 < timeout < math.inf:
        raise ValueError("invalid engine timeout")
    return EngineSettings(model, timeout)


def data_dir() -> Path:
    configured = os.environ.get("JAVIS_DATA_DIR")
    path = Path(configured).expanduser() if configured else Path.home() / ".javis"
    path = path.resolve()
    checkout = Path(__file__).resolve().parents[1]
    if (checkout / ".git").exists() and path.is_relative_to(checkout):
        raise ValueError("the data directory must be outside the javis repository")
    return path
