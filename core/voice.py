"""Optional, direction-specific speech providers."""

import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, overload

from ._process import process_env, run_bounded
from .config import VoiceSettings, VoiceSettingsError, data_dir, load_voice_settings

Direction = Literal["tts", "stt"]
LOCAL_TIMEOUT_S = 30
_EXTENSIONS = {"audio/wav": ".wav", "audio/aiff": ".aiff", "audio/mpeg": ".mp3"}
_WINDOWS_SCRIPT = """
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $synth.SetOutputToWaveFile([IO.Path]::Combine((Get-Location).Path, 'speech.wav'))
    $synth.Speak([Console]::In.ReadToEnd())
} finally {
    $synth.Dispose()
}
"""


@dataclass(frozen=True)
class Audio:
    data: bytes
    media_type: str


class TTSProvider(Protocol):
    def synthesize(self, text: str) -> Audio: ...


class STTProvider(Protocol):
    def transcribe(self, audio: Audio) -> str: ...


@dataclass(frozen=True)
class SettingsFactory[SpeechProvider]:
    create: Callable[[VoiceSettings], SpeechProvider]


@dataclass(frozen=True)
class Provider:
    tts: Callable[[], TTSProvider] | SettingsFactory[TTSProvider] | None = None
    stt: Callable[[], STTProvider] | SettingsFactory[STTProvider] | None = None


@dataclass(frozen=True)
class LocalTTS:
    platform: str
    executable: str

    def synthesize(self, text: str) -> Audio:
        with tempfile.TemporaryDirectory(prefix="javis-tts-") as folder:
            cwd = Path(folder)
            media_type = "audio/wav"
            payload = text.encode("utf-8")
            if self.platform == "win32":
                options = ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_SCRIPT]
            elif self.platform == "darwin":
                media_type = "audio/aiff"
                (cwd / "speech.txt").write_bytes(payload)
                options = ["-o", "speech.aiff", "-f", "speech.txt"]
                payload = b""
            else:
                options = ["-w", "speech.wav", "--stdin"]
            code, _, _ = run_bounded(
                [self.executable, *options], payload, cwd, process_env(), LOCAL_TIMEOUT_S,
            )
            if code:
                raise RuntimeError("local speech synthesis failed")
            data = (cwd / ("speech" + _EXTENSIONS[media_type])).read_bytes()
            if not data:
                raise RuntimeError("local speech synthesis returned empty audio")
            return Audio(data, media_type)


def _local_tts() -> TTSProvider:
    platform = sys.platform
    command = {"win32": "powershell.exe", "darwin": "say", "linux": "espeak-ng"}.get(platform)
    if command is None:
        raise VoiceSettingsError("local TTS is unsupported on this OS")
    executable = shutil.which(command)
    if executable is None:
        raise VoiceSettingsError(f"local TTS requires {command}")
    return LocalTTS(platform, os.path.abspath(executable))


def _elevenlabs_tts(settings: VoiceSettings) -> TTSProvider:
    from .voice_elevenlabs import ElevenLabsTTS

    return ElevenLabsTTS.create(settings.elevenlabs_tts_model)


def _elevenlabs_stt(settings: VoiceSettings) -> STTProvider:
    from .voice_elevenlabs import ElevenLabsSTT

    return ElevenLabsSTT.create(settings.elevenlabs_stt_model)


PROVIDERS: dict[str, Provider] = {
    "local": Provider(tts=_local_tts),
    "elevenlabs": Provider(tts=SettingsFactory(_elevenlabs_tts), stt=SettingsFactory(_elevenlabs_stt)),
}


@overload
def voice_for(direction: Literal["tts"], settings: VoiceSettings | None = None) -> TTSProvider | None: ...


@overload
def voice_for(direction: Literal["stt"], settings: VoiceSettings | None = None) -> STTProvider | None: ...


def voice_for(direction: Direction, settings: VoiceSettings | None = None) -> TTSProvider | STTProvider | None:
    if settings is None:
        settings = load_voice_settings(data_dir())
    if not settings.enabled or not getattr(settings, direction):
        return None
    name = getattr(settings, f"{direction}_provider")
    provider = PROVIDERS.get(name)
    factory = getattr(provider, direction, None)
    if factory is None:
        raise VoiceSettingsError(f"unsupported voice.{direction}_provider")
    if isinstance(factory, SettingsFactory):
        return factory.create(settings)
    return factory()


def save_audio(directory: Path, audio: Audio) -> Path:
    suffix = _EXTENSIONS.get(audio.media_type)
    if suffix is None or not audio.data:
        raise ValueError("unsupported or empty audio")
    target = directory / "voice"
    if target.resolve() != target:
        raise OSError("voice directory must be inside the data directory")
    target.mkdir(exist_ok=True)
    path = None
    try:
        with tempfile.NamedTemporaryFile(dir=target, prefix="speech-", suffix=suffix, delete=False) as file:
            path = Path(file.name)
            file.write(audio.data)
    except BaseException:
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    return path.relative_to(directory)
