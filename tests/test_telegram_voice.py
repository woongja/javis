from dataclasses import replace
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from threading import Event

import pytest

from channels.telegram import bot as telegram
from core import gateway, voice
from core.approval import ApprovalService
from core.engine import EngineResult
from core.voice import Audio, Provider
from core.voice_elevenlabs import ElevenLabsConfigError, ElevenLabsError
from teams import LocalOnly, Team, registry
from tests.test_telegram import CHAT, TOKEN, FakeEngine, api, bot, owner_update, store, update
from worklog.store import Item, Store

ANSWER = "synthetic answer body"
AUDIO = b"\x00synthetic audio bytes\xff"
FILE_PATH = "voice/synthetic_file.ogg"


@pytest.fixture
def speech(monkeypatch):
    class Speech:
        audio = Audio(AUDIO, "audio/ogg")
        error = None

        def __init__(self):
            self.created = 0
            self.calls = []

        def create(self):
            self.created += 1
            return self

        def synthesize(self, text):
            self.calls.append(text)
            if self.error is not None:
                raise self.error
            return self.audio

    fake = Speech()
    monkeypatch.setitem(voice.PROVIDERS, "local", Provider(tts=fake.create))
    return fake


@pytest.fixture
def enabled(tmp_path, speech):
    (tmp_path / "settings.toml").write_text("[voice]\nenabled = true\ntts = true\n", encoding="utf-8")
    return speech


def methods(api):
    return [method for method, _ in api.requests]


@pytest.mark.parametrize("settings", ["", "enabled = true\n", "tts = true\n"])
def test_voice_off_preserves_text(api, store, tmp_path, speech, settings):
    if settings:
        (tmp_path / "settings.toml").write_text("[voice]\n" + settings, encoding="utf-8")
    api.updates(update())
    api.reply()
    bot(api, store, FakeEngine(ANSWER)).poll_once()

    assert methods(api) == ["getUpdates", "sendMessage"]
    assert api.sent()[0]["text"] == f"1. {ANSWER}\n   기록된 출처 없음\n\n기록 ID 1"
    assert speech.created == 0 and speech.calls == []


@pytest.mark.parametrize(("media_type", "method", "field", "suffix"), [
    ("audio/ogg", "sendVoice", "voice", "ogg"),
    ("audio/mpeg", "sendDocument", "document", "mp3"),
    ("audio/wav", "sendDocument", "document", "wav"),
    ("audio/aiff", "sendDocument", "document", "aiff"),
])
def test_upload_follows_all_text_chunks(api, store, enabled, media_type, method, field, suffix):
    enabled.audio = Audio(AUDIO, media_type)
    answer = ANSWER + "a" * 4500
    item = update()
    api.updates(item, item)
    api.reply()
    api.reply()
    api.add(method)
    bot(api, store, FakeEngine(answer)).poll_once()

    assert methods(api) == ["getUpdates", "sendMessage", "sendMessage", method]
    assert enabled.created == 1 and enabled.calls == [answer]
    assert "".join(body["text"] for body in api.sent()) == f"1. {answer}\n   기록된 출처 없음\n\n기록 ID 1"
    content_type, data = api.requests[-1][1]
    parts = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + data
    )
    chat, attachment = parts.iter_parts()
    assert chat.get_param("name", header="Content-Disposition") == "chat_id"
    assert chat.get_payload(decode=True) == str(CHAT).encode("ascii")
    assert attachment.get_param("name", header="Content-Disposition") == field
    assert attachment.get_filename() == f"reply.{suffix}"
    assert attachment.get_content_type() == media_type
    assert attachment.get_payload(decode=True) == AUDIO


def test_only_successful_web_answers_are_eligible(api, store, enabled, monkeypatch):
    def teams(engine):
        entries = registry(engine)
        entries["/note"] = replace(entries["/note"], availability=None)
        entries["/local"] = Team("local", "", "/local <query>", lambda *_: LocalOnly("local result"))
        return entries

    monkeypatch.setattr(gateway, "registry", teams)

    class ErrorEngine:
        def run(self, text):
            assert text == "web error"
            return EngineResult("error", "timeout", (), None, 0)

    gateway.handle(gateway.Request("telegram", "query"), store, FakeEngine(ANSWER))
    api.updates(
        owner_update(40, "승인 대기"),
        update(id=41, text="기록 검색 query"),
        update(id=42, text="출처 1"),
        update(id=43, text="/brain query"),
        owner_update(44, "/note a draft", message_id=81),
        update(id=45, text="/unknown"),
        update(id=46, text="/local query"),
        update(id=47, text="/web web error"),
        update(id=48, chat=CHAT + 1),
        update(id=49, kind="group"),
    )
    for _ in range(7):
        api.reply()
    bot(api, store, ErrorEngine()).poll_once()

    assert methods(api) == ["getUpdates"] + ["sendMessage"] * 7
    assert enabled.created == 0 and enabled.calls == []
    assert store.get_offset()[0] == 50
    assert store.by_update_key("42:101").ref_id == 1
    assert store.by_update_key("46:101").kind == "local"


def dump(store):
    with store._connect() as db:
        return "\n".join(db.iterdump())


@pytest.mark.parametrize("failure", [None, "synthesis", "http", "redirect", "oversized"])
def test_voice_does_not_write_to_store_or_interrupt_polling(
    api, store, enabled, failure, monkeypatch, caplog, tmp_path,
):
    before_voice = []
    synthesize = enabled.synthesize

    def synthesize_after_delivery(text):
        assert store.pending_deliveries() == []
        assert store.get_offset()[0] == 41
        before_voice.append((dump(store), set(tmp_path.iterdir())))
        return synthesize(text)

    monkeypatch.setattr(enabled, "synthesize", synthesize_after_delivery)
    sensitive = f"{ANSWER} {AUDIO!r} {TOKEN}"
    if failure == "synthesis":
        enabled.error = RuntimeError(sensitive)
    if failure == "oversized":
        monkeypatch.setattr(telegram, "_MAX_VOICE_UPLOAD", 512)
        enabled.audio = Audio(b"a" * 513, "audio/ogg")
    api.updates(update())
    api.reply()
    if failure == "http":
        api.add("sendVoice", 503, {"ok": False, "description": sensitive})
    elif failure == "redirect":
        api.add("sendVoice", 302, headers={"Location": api.url + "/redirect-target"})
    elif failure is None:
        api.add("sendVoice")

    poller = bot(api, store, FakeEngine(ANSWER))
    poller.poll_once()

    expected = ["getUpdates", "sendMessage"]
    if failure not in {"synthesis", "oversized"}:
        expected.append("sendVoice")
    assert methods(api) == expected
    messages = [] if failure is None else [
        "Telegram voice synthesis failed" if failure == "synthesis" else "Telegram voice upload failed"
    ]
    assert caplog.messages == messages
    assert enabled.created == 1 and enabled.calls == [ANSWER]
    persisted = dump(store)
    assert before_voice == [(persisted, set(tmp_path.iterdir()))]
    assert AUDIO.hex() not in persisted.lower() and "synthetic audio bytes" not in persisted
    assert store.by_update_key("40:101").items == (Item(ANSWER, ()),)

    api.updates(update(), update(id=41, text="/brain"))
    api.reply()
    poller.poll_once()
    assert methods(api) == expected + ["getUpdates", "sendMessage"]
    assert store.get_offset()[0] == 42


def test_partial_restart_and_permanent_text_failure_skip_speech(api, store, enabled, tmp_path):
    engine = FakeEngine(ANSWER * 250)
    item = update()
    api.updates(item)
    api.reply()
    api.add("sendMessage", 503)

    def interrupted(_delay):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        bot(api, store, engine, sleep=interrupted).poll_once()
    assert store.pending_deliveries()[0][2] == 1

    api.reply()
    api.updates(item, update(id=41))
    api.reply()
    api.add("sendMessage", 403)
    reopened = Store.open(tmp_path)
    bot(api, reopened, engine).poll_once()

    assert methods(api) == [
        "getUpdates", "sendMessage", "sendMessage",
        "sendMessage", "getUpdates", "sendMessage", "sendMessage",
    ]
    assert api.sent()[1] == api.sent()[2]
    assert enabled.created == 0 and enabled.calls == []
    assert reopened.pending_deliveries() == []
    assert reopened.get_offset()[0] == 42


@pytest.fixture
def transcription(monkeypatch):
    class Transcription:
        draft = "synthetic transcript"
        error = None

        def __init__(self):
            self.created = 0
            self.calls = []

        def create(self):
            self.created += 1
            return self

        def transcribe(self, audio):
            self.calls.append(audio)
            if self.error is not None:
                raise self.error
            return self.draft

    fake = Transcription()
    monkeypatch.setitem(voice.PROVIDERS, "elevenlabs", Provider(stt=fake.create))
    return fake


def set_stt(tmp_path, switches):
    (tmp_path / "settings.toml").write_text(
        '[voice]\nstt_provider = "elevenlabs"\n' + switches, encoding="utf-8",
    )


def audio_update(id=40, *, field="voice", metadata=None, **kwargs):
    attachment = {"file_id": "synthetic-id", "file_size": len(AUDIO), "duration": 120, "mime_type": "audio/ogg"}
    attachment.update(metadata or {})
    return owner_update(id, None, message_id=id, **{field: attachment}, **kwargs)


def file_info(api, path=FILE_PATH):
    api.add("getFile", body={"ok": True, "result": {"file_path": path}})


@pytest.mark.parametrize("switches", [None, "enabled = true\n", "stt = true\n"])
def test_stt_off(api, store, transcription, tmp_path, switches):
    if switches is not None:
        set_stt(tmp_path, switches)
    api.updates(audio_update())
    api.reply()
    bot(api, store, FakeEngine()).poll_once()
    assert methods(api) == ["getUpdates", "sendMessage"]
    assert "음성 입력이 꺼져" in api.sent()[0]["text"]
    assert transcription.created == 0 and transcription.calls == []


def test_stt_checks_chat_sender_and_metadata(api, store, transcription, tmp_path):
    set_stt(tmp_path, "enabled = true\nstt = true\n")
    api.updates(
        audio_update(40, chat=CHAT + 1),
        audio_update(41, kind="group"),
        audio_update(42, sender=CHAT + 1),
        audio_update(43, metadata={"file_size": 10 * 1024 * 1024 + 1}),
        audio_update(44, metadata={"duration": 121}),
        audio_update(45, metadata={"mime_type": "text/plain"}),
        audio_update(46, metadata={"file_size": None}),
    )
    for _ in range(5):
        api.reply()
    bot(api, store, FakeEngine()).poll_once()
    assert methods(api) == ["getUpdates"] + ["sendMessage"] * 5
    assert "소유자" in api.sent()[0]["text"]
    assert all("허용 범위" in reply["text"] for reply in api.sent()[1:])
    assert transcription.created == 0 and transcription.calls == []


@pytest.mark.parametrize("case", ["oversized", "oversized_header", "redirect", "foreign", "traversal"])
def test_stt_refuses_unsafe_downloads(api, store, transcription, tmp_path, caplog, case):
    set_stt(tmp_path, "enabled = true\nstt = true\n")
    api.updates(audio_update())
    if case == "foreign":
        file_info(api, f"https://example.invalid/{TOKEN}/{FILE_PATH}")
    elif case == "traversal":
        file_info(api, "voice/../" + FILE_PATH)
    else:
        file_info(api)
        if case == "redirect":
            api.add("GET", 302, headers={"Location": api.url + "/redirect-target"})
        else:
            headers = {"Content-Length": str(len(AUDIO) + 1)} if case == "oversized_header" else None
            api.add("GET", body=AUDIO + b"!", headers=headers)
    api.reply()
    bot(api, store, FakeEngine()).poll_once()
    downloads = [(method, path) for method, path in api.requests if method == "GET"]
    assert downloads == ([] if case in {"foreign", "traversal"} else [("GET", f"/file/bot{TOKEN}/{FILE_PATH}")])
    assert "다운로드하지 못했습니다" in api.sent()[0]["text"]
    assert transcription.calls == []
    output = caplog.text + api.sent()[0]["text"] + dump(store)
    assert TOKEN not in output and FILE_PATH not in output


def test_stt_download_deadline(api, store, transcription, tmp_path, monkeypatch):
    set_stt(tmp_path, "enabled = true\nstt = true\n")
    finish = Event()

    def stalled_body():
        finish.wait(timeout=5)
        return b""

    monkeypatch.setattr(telegram, "_VOICE_DOWNLOAD_TIMEOUT", 1)
    api.updates(audio_update())
    file_info(api)
    api.add("GET", body=stalled_body, headers={"Content-Length": str(len(AUDIO))})
    api.reply()
    try:
        bot(api, store, FakeEngine()).poll_once()
    finally:
        finish.set()
    assert methods(api) == ["getUpdates", "getFile", "GET", "sendMessage"]
    assert "시간이 초과" in api.sent()[0]["text"]
    assert transcription.calls == []


@pytest.mark.parametrize("draft", ["synthetic transcript", "승인 {id}", "/note x", "기록 검색 y", "/web z", "😀" * 4100])
def test_stt_sends_only_a_draft(api, store, enabled, transcription, tmp_path, monkeypatch, draft):
    set_stt(tmp_path, "enabled = true\nstt = true\ntts = true\n")
    proposal = ApprovalService(store).propose("note.create", "draft", str(CHAT), "telegram", datetime.now(UTC))
    transcription.draft = draft.format(id=proposal.id)
    before_files = set(tmp_path.rglob("*"))
    engine = FakeEngine()
    monkeypatch.setattr(telegram, "handle", lambda *args: pytest.fail("gateway called"))
    monkeypatch.setattr(ApprovalService, "decide", lambda *args: pytest.fail("approval decision called"))
    monkeypatch.setattr(store, "search", lambda *args: pytest.fail("search called"))
    item = audio_update(field="audio", metadata={"mime_type": "audio/mp4"})
    api.updates(item, item)
    file_info(api)
    api.add("GET", body=AUDIO, headers={"Content-Length": str(len(AUDIO))})
    api.reply()
    bot(api, store, engine).poll_once()

    assert methods(api) == ["getUpdates", "getFile", "GET", "sendMessage"]
    assert api.requests[1:3] == [("getFile", {"file_id": "synthetic-id"}), ("GET", f"/file/bot{TOKEN}/{FILE_PATH}")]
    reply = api.sent()[0]["text"]
    expected = transcription.draft if len(draft) < 100 else "😀" * 10
    assert reply.startswith("전사 초안: " + expected)
    assert reply.endswith("질문하려면 이 내용을 텍스트로 보내세요.")
    assert len(reply.encode("utf-16-le")) // 2 <= telegram._CHUNK_LIMIT
    assert transcription.created == 1 and transcription.calls == [Audio(AUDIO, "audio/mp4")]
    assert engine.calls == enabled.calls == store.pending_deliveries() == [] and enabled.created == 0
    assert store.get_offset()[0] == 41
    assert store.get_approval(proposal.id) == proposal
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 1
    assert set(tmp_path.rglob("*")) == before_files
    persisted = dump(store)
    assert transcription.draft not in persisted and AUDIO.hex() not in persisted
    assert Store.search(store, "synthetic transcript")[1] == []
    record = store.by_update_key("40:101")
    assert (record.kind, record.request, record.items, record.reason) == ("notice", "", (), "voice_attempted")


@pytest.mark.parametrize(("code", "message"), [
    ("config", "음성 입력 설정"), ("format", "음성 형식"),
    ("timeout", "시간이 초과"), ("provider", "전사 서비스에 오류"),
])
def test_stt_failure_notices_are_safe(api, store, transcription, tmp_path, caplog, capsys, code, message):
    set_stt(tmp_path, "enabled = true\nstt = true\n")
    key = "synthetic-provider-value"
    sensitive = f"{TOKEN} {FILE_PATH} {key}"
    transcription.error = ElevenLabsConfigError(sensitive) if code == "config" else ElevenLabsError(code, sensitive)
    api.updates(audio_update())
    file_info(api)
    api.add("GET", body=AUDIO)
    api.reply()
    bot(api, store, FakeEngine()).poll_once()
    notice = api.sent()[0]["text"]
    assert message in notice
    captured = capsys.readouterr()
    output = notice + dump(store) + caplog.text + captured.out + captured.err
    assert all(value not in output for value in (TOKEN, FILE_PATH, key))


@pytest.mark.parametrize("interrupted", [False, True])
def test_stt_failed_reply_is_not_replayed(api, store, transcription, tmp_path, monkeypatch, interrupted):
    set_stt(tmp_path, "enabled = true\nstt = true\n")
    item = audio_update()
    api.updates(item)
    file_info(api)
    api.add("GET", body=AUDIO)
    poller = bot(api, store, FakeEngine())
    expected = ["getUpdates", "getFile", "GET"]
    if interrupted:
        respond = poller._response

        def interrupt_reply(method, payload):
            if method == "sendMessage":
                raise KeyboardInterrupt
            return respond(method, payload)

        monkeypatch.setattr(poller, "_response", interrupt_reply)
        with pytest.raises(KeyboardInterrupt):
            poller.poll_once()
        assert store.get_offset() is None
    else:
        api.add("sendMessage", 503)
        poller.poll_once()
        expected.append("sendMessage")

    api.updates(item)
    reopened = Store.open(tmp_path)
    bot(api, reopened, FakeEngine()).poll_once()
    assert methods(api) == expected + ["getUpdates"]
    assert reopened.get_offset()[0] == 41
    assert reopened.pending_deliveries() == [] and transcription.created == 1
