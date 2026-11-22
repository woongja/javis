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


@dataclass(frozen=True)
class VoiceSettings:
    enabled: bool = False
    tts: bool = False
    stt: bool = False
    tts_provider: str = "local"
    stt_provider: str = "none"
    elevenlabs_tts_model: str = "eleven_multilingual_v2"
    elevenlabs_stt_model: str = "scribe_v1"


class VoiceSettingsError(ValueError):
    pass


def _voice_values(document: dict) -> dict:
    values = document.get("voice", {})
    if not isinstance(values, dict):
        raise VoiceSettingsError("[voice] must be a table")
    for key, value in values.items():
        if key in ("enabled", "tts", "stt"):
            if type(value) is not bool:
                raise VoiceSettingsError(f"voice.{key} must be a boolean")
        elif key in ("tts_provider", "stt_provider", "elevenlabs_tts_model", "elevenlabs_stt_model"):
            if not isinstance(value, str) or not value.strip():
                raise VoiceSettingsError(f"voice.{key} must be a nonempty string")
        else:
            raise VoiceSettingsError("unknown key in [voice]")
    return values


def load_voice_settings(directory: Path) -> VoiceSettings:
    try:
        with files("core").joinpath("settings.toml").open("rb") as source:
            defaults = _voice_values(tomllib.load(source))
        try:
            contents = (directory / "settings.toml").read_bytes().decode("utf-8-sig")
            overrides = _voice_values(tomllib.loads(contents))
        except FileNotFoundError:
            overrides = {}
    except (OSError, tomllib.TOMLDecodeError, UnicodeError):
        raise VoiceSettingsError("could not read voice settings.toml") from None
    return VoiceSettings(**(defaults | overrides))


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
