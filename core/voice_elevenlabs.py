"""In-memory speech synthesis and transcription over direct HTTPS."""

import http.client
import json
import os
import re
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from harness import HarnessError
from harness.paths import env_value, repo_root

from .config import VoiceSettingsError
from .voice import Audio, Direction

HOST = "api.elevenlabs.io"
CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 30
MAX_TEXT_LENGTH = 10_000
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_AUDIO_BYTES + 4096
MAX_TTS_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_STT_RESPONSE_BYTES = 2 * 1024 * 1024
_MEDIA_EXTENSIONS = {
    "audio/aac": "aac", "audio/aiff": "aiff", "audio/flac": "flac",
    "audio/mp4": "m4a", "audio/mpeg": "mp3", "audio/ogg": "ogg",
    "audio/opus": "opus", "audio/wav": "wav", "audio/webm": "webm",
    "audio/x-aac": "aac", "audio/x-aiff": "aiff", "audio/x-flac": "flac",
    "audio/x-m4a": "m4a", "audio/x-wav": "wav", "audio/mp3": "mp3",
    "audio/mpeg3": "mp3", "audio/x-mpeg-3": "mp3",
}
_API_KEY = "ELEVENLABS_API_KEY"
_VOICE_ID = "ELEVENLABS_VOICE_ID"
_CREDENTIAL_PATTERNS = {_API_KEY: r"[!-~]{1,512}", _VOICE_ID: r"[A-Za-z0-9_-]{1,128}"}


class ElevenLabsConfigError(VoiceSettingsError):
    code = "config"


class ElevenLabsError(RuntimeError):
    def __init__(self, code: Literal["format", "timeout", "provider"], message: str):
        self.code = code
        super().__init__(message)


def _setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        checkout = Path(__file__).resolve().parents[1]
        if not (checkout / ".git").exists():
            checkout = repo_root()
        return env_value(checkout, name, env={}) or ""
    except (HarnessError, OSError, ValueError):
        return ""


def _credentials(direction: Direction) -> dict[str, str]:
    names = (_API_KEY, _VOICE_ID) if direction == "tts" else (_API_KEY,)
    return {name: _setting(name) for name in names}


def _validate_credentials(values: dict[str, str]) -> None:
    for name, value in values.items():
        if not value:
            raise ElevenLabsConfigError(f"{name} is missing")
        if not re.fullmatch(_CREDENTIAL_PATTERNS[name], value):
            raise ElevenLabsConfigError(f"{name} has an invalid format")


def credential_status(direction: Direction) -> tuple[dict[str, bool], bool]:
    values = _credentials(direction)
    presence = {name: bool(value) for name, value in values.items()}
    try:
        _validate_credentials(values)
    except ElevenLabsConfigError:
        return presence, False
    return presence, True


def _post(path: str, api_key: str, body: bytes, content_type: str, accept: str, limit: int) -> bytes:
    if len(body) > MAX_REQUEST_BYTES:
        raise ElevenLabsError("provider", "ElevenLabs request exceeds the size limit")
    headers = {"xi-api-key": api_key, "Content-Type": content_type, "Accept": accept}
    try:
        # Direct HTTPS ignores proxy environment variables and never follows redirects.
        with closing(http.client.HTTPSConnection(HOST, timeout=CONNECT_TIMEOUT_S)) as connection:
            connection.connect()
            connection.sock.settimeout(READ_TIMEOUT_S)
            connection.request("POST", path, body=body, headers=headers)
            with closing(connection.getresponse()) as response:
                if not 200 <= response.status < 300:
                    raise ElevenLabsError("provider", "ElevenLabs returned an HTTP error")
                if response.length is not None and response.length > limit:
                    raise ElevenLabsError("provider", "ElevenLabs response exceeds the size limit")
                media_type = response.getheader("Content-Type", "").partition(";")[0].strip().lower()
                if media_type != accept:
                    raise ElevenLabsError("format", "ElevenLabs returned an unexpected content type")
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise ElevenLabsError("provider", "ElevenLabs response exceeds the size limit")
                if not data or response.length:
                    raise ElevenLabsError("format", "ElevenLabs returned an empty or incomplete response")
                return data
    except TimeoutError:
        raise ElevenLabsError("timeout", "ElevenLabs request timed out") from None
    except http.client.IncompleteRead:
        raise ElevenLabsError("format", "ElevenLabs returned an incomplete response") from None
    except (OSError, http.client.HTTPException):
        raise ElevenLabsError("provider", "ElevenLabs connection failed") from None


@dataclass(frozen=True)
class ElevenLabsTTS:
    api_key: str = field(repr=False)
    voice_id: str = field(repr=False)
    model: str

    def __post_init__(self) -> None:
        _validate_credentials({_API_KEY: self.api_key, _VOICE_ID: self.voice_id})

    @classmethod
    def create(cls, model: str) -> "ElevenLabsTTS":
        values = _credentials("tts")
        return cls(values[_API_KEY], values[_VOICE_ID], model)

    def synthesize(self, text: str) -> Audio:
        if len(text) > MAX_TEXT_LENGTH:
            raise ElevenLabsError("provider", "ElevenLabs text exceeds the size limit")
        body = json.dumps({"text": text, "model_id": self.model}).encode("utf-8")
        data = _post(
            f"/v1/text-to-speech/{self.voice_id}", self.api_key, body,
            "application/json", "audio/mpeg", MAX_TTS_RESPONSE_BYTES,
        )
        return Audio(data, "audio/mpeg")


@dataclass(frozen=True)
class ElevenLabsSTT:
    api_key: str = field(repr=False)
    model: str

    def __post_init__(self) -> None:
        _validate_credentials({_API_KEY: self.api_key})

    @classmethod
    def create(cls, model: str) -> "ElevenLabsSTT":
        return cls(_setting(_API_KEY), model)

    def transcribe(self, audio: Audio) -> str:
        if len(audio.data) > MAX_AUDIO_BYTES:
            raise ElevenLabsError("provider", "ElevenLabs audio exceeds the size limit")
        media_type = audio.media_type.partition(";")[0].strip().lower()
        extension = _MEDIA_EXTENSIONS.get(media_type)
        if not extension or not audio.data:
            raise ElevenLabsError("format", "ElevenLabs requires a supported nonempty audio file")
        boundary = uuid.uuid4().hex
        fields = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"model_id\"\r\n\r\n"
            f"{self.model}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"voice.{extension}\"\r\n"
            f"Content-Type: {media_type}\r\n\r\n"
        ).encode("utf-8")
        body = b"".join((fields, audio.data, f"\r\n--{boundary}--\r\n".encode("ascii")))
        data = _post(
            "/v1/speech-to-text", self.api_key, body,
            f"multipart/form-data; boundary={boundary}", "application/json", MAX_STT_RESPONSE_BYTES,
        )
        try:
            result = json.loads(data.decode("utf-8"))
        except (ValueError, RecursionError):
            raise ElevenLabsError("format", "ElevenLabs returned an invalid transcript") from None
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise ElevenLabsError("format", "ElevenLabs returned an invalid transcript")
        return result["text"]
