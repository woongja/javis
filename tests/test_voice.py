from pathlib import Path

import pytest

from core import cli, voice
from core.config import VoiceSettings, VoiceSettingsError, load_voice_settings
from core.engine import EngineResult
from worklog.store import Item, Store


def configure(directory, values):
    (directory / "settings.toml").write_text(f"[voice]\n{values}", encoding="utf-8")


@pytest.fixture
def cli_voice(tmp_path, monkeypatch):
    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path))
    calls = []

    class Engine:
        def __init__(self, settings):
            pass

        def run(self, text):
            return EngineResult("ok", None, (Item("web answer", ("https://example.org",)),), None, 0)

    class Speech:
        def synthesize(self, text):
            calls.append(text)
            return voice.Audio(b"fake-audio", "audio/wav")

    def create():
        calls.append("create")
        return Speech()

    monkeypatch.setattr(cli, "ClaudeEngine", Engine)
    monkeypatch.setitem(voice.PROVIDERS, "local", voice.Provider(tts=create))
    return calls


@pytest.mark.parametrize("values", [None, "enabled = true", "tts = true"])
def test_disabled_voice_leaves_web_answer_alone(tmp_path, cli_voice, values, capsys):
    if values is not None:
        configure(tmp_path, values)
    assert cli.main(["ask", "question"]) == 0
    assert capsys.readouterr().out.startswith("1. web answer")
    assert cli.main(["voice", "status"]) == 0
    assert "available=no (disabled)" in capsys.readouterr().out
    assert cli_voice == []
    assert not (tmp_path / "voice").exists()


def test_enabled_voice_saves_a_new_relative_path_for_each_answer(tmp_path, cli_voice, capsys):
    configure(tmp_path, "enabled = true\ntts = true")
    paths = []
    for _ in range(2):
        assert cli.main(["ask", "question"]) == 0
        output = capsys.readouterr()
        assert not output.err
        path = Path(output.out.splitlines()[-1])
        assert path.parent == Path("voice") and path.suffix == ".wav"
        assert (tmp_path / path).read_bytes() == b"fake-audio"
        paths.append(path)
    assert len(set(paths)) == 2
    assert sorted((tmp_path / "voice").iterdir()) == sorted(tmp_path / path for path in paths)
    assert cli_voice == ["create", "web answer"] * 2
    assert b"fake-audio" not in (tmp_path / "javis.db").read_bytes()
    assert all(record.items[0].text == "web answer" for record in Store.open(tmp_path).recent(2))
    assert cli.main(["voice", "status"]) == 0
    assert "tts=true provider='local' available=yes" in capsys.readouterr().out
    assert cli_voice[-1] == "create"


def test_local_approval_search_and_notices_never_speak(tmp_path, monkeypatch, cli_voice):
    data = tmp_path / "data"
    data.mkdir()
    configure(data, "enabled = true\ntts = true")
    monkeypatch.setenv("JAVIS_DATA_DIR", str(data))
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("private result", encoding="utf-8")
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(vault))
    monkeypatch.setattr(cli, "ClaudeEngine", lambda settings: None)
    assert cli.main(["ask", "/brain private"]) == 0
    assert cli.main(["ask", "/note private draft"]) == 0
    assert cli.main(["search", "private"]) == 0
    assert cli.main(["ask", "/unknown"]) == 1
    assert cli.main(["ask", "출처 1"]) == 1
    assert cli_voice == []
    assert not (data / "voice").exists()


@pytest.mark.parametrize(("values", "message"), [
    ('tts = true\ntts_provider = "missing"', "unsupported voice.tts_provider"),
    ('stt = true\nstt_provider = "local"', "unsupported voice.stt_provider"),
    ("tts = true", "unsupported on this OS"),
])
def test_status_reports_unsupported_configuration(tmp_path, monkeypatch, capsys, values, message):
    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(voice.sys, "platform", "unsupported")
    monkeypatch.setattr(voice.shutil, "which", lambda name: pytest.fail("unexpected fallback"))
    configure(tmp_path, "enabled = true\n" + values)
    assert cli.main(["voice", "status"]) == 1
    assert message in capsys.readouterr().out
    assert not (tmp_path / "javis.db").exists()


@pytest.mark.parametrize(("platform", "command", "suffix", "media_type"), [
    ("win32", "powershell.exe", "wav", "audio/wav"),
    ("darwin", "say", "aiff", "audio/aiff"),
    ("linux", "espeak-ng", "wav", "audio/wav"),
])
def test_local_transport_keeps_text_out_of_arguments(tmp_path, monkeypatch, platform, command, suffix, media_type):
    text = """한글 "quoted" 'text' $(expression);\nnext line"""
    monkeypatch.setattr(voice.sys, "platform", platform)
    monkeypatch.setattr(voice.shutil, "which", lambda name: str(tmp_path / name))
    monkeypatch.setattr(voice.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-placeholder")

    def run(args, payload, cwd, env, timeout):
        assert args[0] == str(tmp_path / command)
        assert "quoted" not in " ".join(args) and "expression" not in " ".join(args)
        assert timeout == voice.LOCAL_TIMEOUT_S == 30
        assert "ELEVENLABS_API_KEY" not in env
        if platform == "darwin":
            assert args[1:] == ["-o", "speech.aiff", "-f", "speech.txt"]
            assert not payload and (cwd / "speech.txt").read_bytes() == text.encode("utf-8")
        else:
            assert payload == text.encode("utf-8")
            if platform == "win32":
                assert "System.Speech.Synthesis.SpeechSynthesizer" in args[-1]
                assert "[Console]::In.ReadToEnd()" in args[-1]
            else:
                assert args[1:] == ["-w", "speech.wav", "--stdin"]
        (cwd / f"speech.{suffix}").write_bytes(b"fake-audio")
        return 0, b"", b""

    monkeypatch.setattr(voice, "run_bounded", run)
    provider = voice.voice_for("tts", VoiceSettings(enabled=True, tts=True))
    assert provider.synthesize(text) == voice.Audio(b"fake-audio", media_type)
    assert not list(tmp_path.iterdir())


def test_voice_settings_overrides_and_validation(tmp_path):
    assert load_voice_settings(tmp_path) == VoiceSettings()
    configure(tmp_path, 'enabled = true\ntts_provider = "test"')
    assert load_voice_settings(tmp_path) == VoiceSettings(enabled=True, tts_provider="test")
    for values, message in [
        ('tts = "true"', "must be a boolean"), ("extra = true", "unknown key"),
        ('elevenlabs_tts_model = " "', "must be a nonempty string"),
    ]:
        configure(tmp_path, values)
        with pytest.raises(VoiceSettingsError, match=message):
            load_voice_settings(tmp_path)


def test_voice_settings_accepts_utf8_bom_and_model_overrides(tmp_path):
    (tmp_path / "settings.toml").write_text(
        '[voice]\nenabled = true\nelevenlabs_tts_model = "custom_tts"\nelevenlabs_stt_model = "custom_stt"\n',
        encoding="utf-8-sig",
    )
    assert load_voice_settings(tmp_path) == VoiceSettings(
        enabled=True, elevenlabs_tts_model="custom_tts", elevenlabs_stt_model="custom_stt",
    )


def test_synthesis_failure_preserves_web_answer(tmp_path, cli_voice, monkeypatch, capsys):
    configure(tmp_path, "enabled = true\ntts = true")

    class BrokenSpeech:
        def synthesize(self, text):
            raise RuntimeError("private diagnostic\n" + text)

    monkeypatch.setitem(voice.PROVIDERS, "local", voice.Provider(tts=BrokenSpeech))
    assert cli.main(["ask", "question"]) == 0
    output = capsys.readouterr()
    assert output.out.startswith("1. web answer")
    assert output.err.splitlines() == ["javis: voice synthesis failed"]
    assert not (tmp_path / "voice").exists()
