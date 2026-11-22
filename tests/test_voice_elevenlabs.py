import io
import json
import traceback
from dataclasses import replace
from email import policy
from email.parser import BytesParser

import pytest

from core import cli, voice
from core import voice_elevenlabs as adapter
from core.config import VoiceSettings

FAKE_KEY = "fake-test-credential"
FAKE_VOICE = "sample_Voice-42"
SETTINGS = VoiceSettings(enabled=True, tts=True, stt=True, tts_provider="elevenlabs", stt_provider="elevenlabs")


@pytest.fixture(autouse=True)
def fake_credentials(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", FAKE_KEY)
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", FAKE_VOICE)
    monkeypatch.setattr(adapter, "env_value", lambda *args, **kwargs: pytest.fail("unexpected .env lookup"))
    monkeypatch.setattr(
        adapter.http.client, "HTTPSConnection", lambda *args, **kwargs: pytest.fail("unexpected HTTPS connection"),
    )


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"fake mp3", *, status=200, media_type="audio/mpeg", length=None):
        super().__init__(body)
        self.status = status
        self.length = length
        self.media_type = media_type
        self.read_sizes = []

    def getheader(self, name, default=None):
        assert name == "Content-Type"
        return self.media_type

    def read(self, size):
        self.read_sizes.append(size)
        result = super().read(size)
        if self.length is not None:
            self.length -= len(result)
        return result


@pytest.fixture
def fake_https(monkeypatch):
    def install(response, *, failure=None):
        calls = []

        class Connection:
            def __init__(self, host, *, timeout):
                calls.append((host, timeout))
                self.sock = self

            def connect(self):
                if failure is not None:
                    raise failure

            def settimeout(self, timeout):
                calls.append(timeout)

            def request(self, method, path, *, body, headers):
                calls.append((method, path, body, headers))

            def getresponse(self):
                return response

            def close(self):
                calls.append("closed")

        monkeypatch.setattr(adapter.http.client, "HTTPSConnection", Connection)
        return calls

    return install


def test_tts_https_request_and_mp3(tmp_path, fake_https, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    response = FakeResponse(length=8)
    calls = fake_https(response)
    provider = voice.voice_for("tts", replace(SETTINGS, elevenlabs_tts_model="custom_tts"))
    audio = provider.synthesize("한글 text")
    assert audio == voice.Audio(b"fake mp3", "audio/mpeg")
    assert calls[:2] == [("api.elevenlabs.io", adapter.CONNECT_TIMEOUT_S), adapter.READ_TIMEOUT_S]
    method, path, body, headers = calls[2]
    assert (method, path) == ("POST", f"/v1/text-to-speech/{FAKE_VOICE}")
    assert headers == {"xi-api-key": FAKE_KEY, "Content-Type": "application/json", "Accept": "audio/mpeg"}
    assert json.loads(body) == {"text": "한글 text", "model_id": "custom_tts"}
    assert calls[3:] == ["closed"] and response.closed
    path = voice.save_audio(tmp_path, audio)
    assert path.suffix == ".mp3" and (tmp_path / path).read_bytes() == audio.data


def test_stt_multipart_and_transcription(fake_https):
    response = FakeResponse(b'{"text":"hello", "words": []}', media_type="application/json; charset=utf-8")
    calls = fake_https(response)
    provider = voice.voice_for("stt", replace(SETTINGS, elevenlabs_stt_model="custom_stt"))
    assert provider.transcribe(voice.Audio(b"OggS\x00\xff", "audio/ogg")) == "hello"
    assert calls[0][0] == "api.elevenlabs.io"
    method, path, body, headers = calls[2]
    assert (method, path) == ("POST", "/v1/speech-to-text")
    assert headers["xi-api-key"] == FAKE_KEY
    assert headers["Accept"] == "application/json"
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {headers['Content-Type']}\r\n\r\n".encode("ascii") + body,
    )
    model, audio = message.iter_parts()
    assert model.get_param("name", header="Content-Disposition") == "model_id"
    assert model.get_payload(decode=True) == b"custom_stt"
    assert audio.get_param("name", header="Content-Disposition") == "file"
    assert audio.get_filename() == "voice.ogg"
    assert audio.get_content_type() == "audio/ogg"
    assert audio.get_payload(decode=True) == b"OggS\x00\xff"


@pytest.mark.parametrize("status", [302, 307, 401, 429, 500])
def test_http_errors_close_response_without_reading_or_following(fake_https, status):
    response = FakeResponse(FAKE_KEY.encode(), status=status)
    response.getheader = {"Location": "https://redirect.invalid/path?secret=private-query"}.get
    calls = fake_https(response)
    with pytest.raises(adapter.ElevenLabsError) as error:
        voice.voice_for("tts", SETTINGS).synthesize("text")
    assert error.value.code == "provider"
    assert FAKE_KEY not in str(error.value)
    assert "private-query" not in str(error.value)
    assert len(calls) == 4 and calls[0][0] == adapter.HOST and calls[-1] == "closed"
    assert response.read_sizes == [] and response.closed


@pytest.mark.parametrize("stage", ["connect", "read"])
def test_timeout_is_sanitized(fake_https, stage):
    failure = TimeoutError(f"private URL query and response {FAKE_KEY}")
    response = FakeResponse()

    def read(size):
        raise failure

    if stage == "read":
        response.read = read
    calls = fake_https(response, failure=failure if stage == "connect" else None)
    with pytest.raises(adapter.ElevenLabsError) as error:
        voice.voice_for("tts", SETTINGS).synthesize("text")
    assert error.value.code == "timeout"
    assert FAKE_KEY not in "".join(traceback.format_exception(error.value))
    assert calls[-1] == "closed"


@pytest.mark.parametrize("length", [None, 200])
def test_large_response_aborts_at_limit(monkeypatch, fake_https, length):
    monkeypatch.setattr(adapter, "MAX_TTS_RESPONSE_BYTES", 4)
    response = FakeResponse(b"oversized response", length=length)
    calls = fake_https(response)
    with pytest.raises(adapter.ElevenLabsError) as error:
        voice.voice_for("tts", SETTINGS).synthesize("text")
    assert error.value.code == "provider"
    assert response.read_sizes == ([5] if length is None else [])
    assert response.closed and calls[-1] == "closed"


def test_input_rejected_before_connecting(monkeypatch):
    monkeypatch.setattr(adapter, "MAX_TEXT_LENGTH", 3)
    monkeypatch.setattr(adapter, "MAX_AUDIO_BYTES", 3)
    with pytest.raises(adapter.ElevenLabsError) as text_error:
        voice.voice_for("tts", SETTINGS).synthesize("long")
    with pytest.raises(adapter.ElevenLabsError) as audio_error:
        voice.voice_for("stt", SETTINGS).transcribe(voice.Audio(b"long", "audio/ogg"))
    with pytest.raises(adapter.ElevenLabsError) as mime_error:
        voice.voice_for("stt", SETTINGS).transcribe(voice.Audio(b"x", "text/plain"))
    assert (text_error.value.code, audio_error.value.code, mime_error.value.code) == ("provider", "provider", "format")


@pytest.mark.parametrize("body", [b"", b"\xff", b"{broken", b'[]', b'{"text": null}'])
def test_invalid_transcript_is_format_error(fake_https, body):
    fake_https(FakeResponse(body, media_type="application/json"))
    with pytest.raises(adapter.ElevenLabsError) as error:
        voice.voice_for("stt", SETTINGS).transcribe(voice.Audio(b"audio", "audio/ogg"))
    assert error.value.code == "format"


def test_invalid_audio_response_is_format_error(fake_https):
    fake_https(FakeResponse(FAKE_KEY.encode(), media_type="text/html"))
    with pytest.raises(adapter.ElevenLabsError) as error:
        voice.voice_for("tts", SETTINGS).synthesize("text")
    assert error.value.code == "format"
    assert FAKE_KEY not in str(error.value)


@pytest.mark.parametrize("direction", ["tts", "stt"])
def test_missing_key_is_config_error_and_fallback_is_scoped(monkeypatch, direction):
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    calls = []

    def fallback(root, key, *, env):
        assert (root / ".git").exists() and env == {}
        calls.append(key)
        return None

    monkeypatch.setattr(adapter, "env_value", fallback)
    with pytest.raises(adapter.ElevenLabsConfigError) as error:
        voice.voice_for(direction, SETTINGS)
    assert error.value.code == "config" and calls == ["ELEVENLABS_API_KEY"]
    assert FAKE_VOICE not in str(error.value)
    monkeypatch.setattr(adapter, "env_value", lambda root, key, *, env: FAKE_KEY)
    assert voice.voice_for(direction, SETTINGS) is not None


@pytest.mark.parametrize("value", ["", "voice/id", "..", "voice?key=private"])
def test_only_safe_voice_ids_are_accepted(monkeypatch, value):
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", value)
    monkeypatch.setattr(adapter, "env_value", lambda *args, **kwargs: None)
    with pytest.raises(adapter.ElevenLabsConfigError) as error:
        voice.voice_for("tts", SETTINGS)
    assert error.value.code == "config" and "ELEVENLABS_VOICE_ID" in str(error.value)
    assert "private" not in str(error.value) and FAKE_KEY not in str(error.value)
    assert voice.voice_for("stt", SETTINGS) is not None


def test_disabled_directions_skip_credentials_and_tts_reads_once(tmp_path, monkeypatch, capsys):
    calls = []
    getenv = adapter.os.environ.get

    def tracked_get(name, default=None):
        if name.startswith("ELEVENLABS_"):
            calls.append(name)
        return getenv(name, default)

    monkeypatch.setattr(adapter.os.environ, "get", tracked_get)
    for direction in ("tts", "stt"):
        assert voice.voice_for(direction, replace(SETTINGS, enabled=False)) is None
    (tmp_path / "settings.toml").write_text(
        '[voice]\ntts = true\nstt = true\ntts_provider = "elevenlabs"\nstt_provider = "elevenlabs"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path))
    assert cli.main(["voice", "status"]) == 0
    assert "ELEVENLABS_" not in capsys.readouterr().out
    tts_only = replace(SETTINGS, stt=False)
    assert voice.voice_for("stt", tts_only) is None
    assert calls == []
    assert voice.voice_for("tts", tts_only) is not None
    assert calls == ["ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"]
    calls.clear()
    assert voice.voice_for("stt", replace(SETTINGS, tts=False)) is not None
    assert calls == ["ELEVENLABS_API_KEY"]


def test_voice_status_reports_only_credential_presence(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path))
    (tmp_path / "settings.toml").write_text(
        '[voice]\nenabled = true\ntts = true\nstt = true\n'
        'tts_provider = "elevenlabs"\nstt_provider = "elevenlabs"\n', encoding="utf-8-sig",
    )
    assert cli.main(["voice", "status"]) == 0
    output = capsys.readouterr()
    assert not output.err
    assert output.out.splitlines() == [
        "enabled=true",
        "tts=true provider='elevenlabs' available=yes ELEVENLABS_API_KEY=present ELEVENLABS_VOICE_ID=present",
        "stt=true provider='elevenlabs' available=yes ELEVENLABS_API_KEY=present",
    ]
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    monkeypatch.setattr(adapter, "env_value", lambda *args, **kwargs: None)
    assert cli.main(["voice", "status"]) == 1
    output = capsys.readouterr()
    assert output.out.count("ELEVENLABS_API_KEY=missing") == 2
    assert FAKE_KEY not in output.out + output.err and FAKE_VOICE not in output.out + output.err
