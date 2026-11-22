"""Single-poller Telegram channel with durable reply delivery."""

import http.client
import json
import logging
import random
import re
import socket
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Timer
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request as HTTPRequest, build_opener
from uuid import uuid4

from core.approval import ApprovalService, Executor
from core.config import VoiceSettingsError, data_dir, load_voice_settings
from core.engine import Engine
from core.gateway import Request, handle
from core.note import NOTE_EXECUTORS
from core.voice import Audio, voice_for
from core.voice_elevenlabs import ElevenLabsError
from worklog.store import ApprovalKind, Record, Store

_API_BASE = "https://api.telegram.org"
_POLL_TIMEOUT = 25
_HTTP_TIMEOUT = 35
_MAX_VOICE_UPLOAD = 20 * 1024 * 1024
_MAX_VOICE_DOWNLOAD = 10 * 1024 * 1024
_MAX_VOICE_DURATION = 120
_VOICE_DOWNLOAD_TIMEOUT = 15
_VOICE_INPUT_TYPES = {"audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav"}
_FILE_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")
_STT_ERRORS = {
    "config": "음성 입력 설정을 확인해 주세요.",
    "format": "음성 형식을 처리할 수 없습니다.",
    "timeout": "음성 입력 처리 시간이 초과되었습니다.",
    "provider": "음성 전사 서비스에 오류가 발생했습니다.",
}
# Use sendVoice for OGG/Opus and attach other provider formats as documents.
_VOICE_FILES = {
    "audio/ogg": ("sendVoice", "voice", "reply.ogg"),
    "audio/mpeg": ("sendDocument", "document", "reply.mp3"),
    "audio/wav": ("sendDocument", "document", "reply.wav"),
    "audio/aiff": ("sendDocument", "document", "reply.aiff"),
}
_LOG = logging.getLogger(__name__)
_CURSOR_LIFETIME = timedelta(days=1)
_CHUNK_LIMIT = 4000
# The gateway formats the footer before the real SQLite ID is available.
_DEFERRED_ID = 1000000000000000000
_ID_FOOTER = f"\n\n기록 ID {_DEFERRED_ID}"
_SAFE_ERROR = "응답을 처리하지 못했습니다."
_APPROVAL_ID = re.compile(r"[0-9a-fA-F]{32}")
_APPROVAL_USAGE = "사용법: 승인 대기 / 승인 <ID> / 거절 <ID>"
_PC_ONLY = "brain.import는 PC에서만 확인하고 결정할 수 있습니다."
_DECISION_REPLIES = {
    "claimed": "승인을 접수했습니다.",
    "rejected": "거절했습니다.",
    "expired": "만료된 승인입니다.",
    "not_found": "승인 ID를 찾을 수 없습니다.",
    "forbidden": "이 승인을 결정할 수 없습니다.",
    "already_decided": "이미 결정된 승인입니다.",
    "pc_only": _PC_ONLY,
    "executor_missing": "실행기 미등록 상태입니다.",
}


class TelegramError(Exception):
    def __init__(self, code: int):
        self.code = code
        super().__init__(str(code))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _DeferredSave:
    """Let the gateway read history and stage its record for an atomic delivery save."""

    def __init__(self, store: Store):
        self.store = store
        self.record: Record

    def __getattr__(self, name):
        return getattr(self.store, name)

    def save(self, record: Record) -> int:
        self.record = record
        return _DEFERRED_ID


def _split(text: str) -> list[str]:
    chunks = []
    start = size = 0
    for index, char in enumerate(text):
        units = 2 if ord(char) > 0xFFFF else 1
        if size + units > _CHUNK_LIMIT:
            chunks.append(text[start:index])
            start, size = index, 0
        size += units
    chunks.append(text[start:])
    return chunks


def _reply_chunks(text: str) -> list[str]:
    if not text.endswith(_ID_FOOTER):
        return _split(text)
    body = text.removesuffix(_ID_FOOTER)
    chunks = _split(body)
    if len(chunks[-1].encode("utf-16-le")) // 2 + len(_ID_FOOTER) <= _CHUNK_LIMIT:
        chunks[-1] += _ID_FOOTER
    else:
        chunks.append(_ID_FOOTER)
    return chunks


def _local_now() -> datetime:
    return datetime.now().astimezone()


class TelegramBot:
    def __init__(
        self, token: str, chat_id: int, store: Store, engine: Engine, *,
        api_base: str = _API_BASE,
        now: Callable[[], datetime] = _local_now,
        sleep: Callable[[float], None] = time.sleep,
        executors: Mapping[ApprovalKind, Executor] | None = None,
    ):
        self._token = token
        self._chat_id = chat_id
        self._store = store
        self._engine = engine
        self._approvals = ApprovalService(store, NOTE_EXECUTORS if executors is None else executors)
        self._api_base = api_base.rstrip("/")
        self._now = now
        self._sleep = sleep
        self._http = build_opener(ProxyHandler({}), _NoRedirect())
        saved = store.get_offset()
        self._cursor = (saved[0], datetime.fromisoformat(saved[1])) if saved else None

    def _response(self, method: str, payload: dict) -> tuple[int, dict]:
        return self._post(method, json.dumps(payload).encode("ascii"), "application/json")

    def _post(self, method: str, data: bytes, content_type: str) -> tuple[int, dict]:
        request = HTTPRequest(
            f"{self._api_base}/bot{quote(self._token, safe=':')}/{method}",
            data=data,
            headers={"Content-Type": content_type},
        )
        try:
            response = self._http.open(request, timeout=_HTTP_TIMEOUT)
        except HTTPError as error:
            response = error
        except (OSError, http.client.HTTPException, ValueError):
            return 0, {}

        with response:
            status = response.code
            try:
                body = json.loads(response.read())
            except (OSError, http.client.HTTPException, ValueError):
                body = {}
        return status, body if isinstance(body, dict) else {}

    def _upload_voice(self, audio: Audio) -> None:
        method, field, filename = _VOICE_FILES[audio.media_type]
        boundary = uuid4().hex
        header = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{self._chat_id}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {audio.media_type}\r\n\r\n"
        ).encode("ascii")
        trailer = f"\r\n--{boundary}--\r\n".encode("ascii")
        if not audio.data or len(header) + len(audio.data) + len(trailer) > _MAX_VOICE_UPLOAD:
            raise ValueError("invalid voice upload size")
        # Send once. Text delivery has its own retry and recovery rules.
        status, body = self._post(
            method, b"".join((header, audio.data, trailer)), f"multipart/form-data; boundary={boundary}",
        )
        if not 200 <= status < 300 or body.get("ok") is not True:
            raise TelegramError(status)

    def _send_voice_reply(self, record: Record) -> None:
        try:
            provider = voice_for("tts")
            if provider is None:
                return
            audio = provider.synthesize("\n".join(item.text for item in record.items))
        except Exception:
            _LOG.warning("Telegram voice synthesis failed")
            return
        try:
            self._upload_voice(audio)
        except Exception:
            _LOG.warning("Telegram voice upload failed")

    def _read_voice_file(self, path: str, limit: int, deadline: float, payload: bytes | None = None) -> bytes:
        base = urlsplit(self._api_base)
        if base.scheme == "https" and base.hostname:
            connect = http.client.HTTPSConnection
        elif base.scheme == "http" and base.hostname == "127.0.0.1":
            # The injected local fake API also serves the file route.
            connect = http.client.HTTPConnection
        else:
            raise ValueError("invalid Telegram origin")

        def remaining() -> float:
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise TimeoutError
            return seconds

        with closing(connect(base.hostname, base.port, timeout=remaining())) as connection:
            connection.connect()
            transport = connection.sock

            def interrupt() -> None:
                try:
                    transport.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            # Enforce the deadline even if the server trickles headers or body bytes.
            timer = Timer(remaining(), interrupt)
            timer.daemon = True
            timer.start()
            try:
                method = "GET" if payload is None else "POST"
                headers = {} if payload is None else {"Content-Type": "application/json"}
                connection.request(method, base.path + path, body=payload, headers=headers)
                with closing(connection.getresponse()) as response:
                    if response.status != 200 or (response.length is not None and response.length > limit):
                        raise ValueError("invalid Telegram file response")
                    data = response.read(limit + 1)
                    if not data or len(data) > limit or response.length:
                        raise ValueError("invalid Telegram file size")
                    return data
            finally:
                timer.cancel()
                remaining()

    def _download_voice(self, file_id: str, size: int, media_type: str) -> Audio:
        deadline = time.monotonic() + _VOICE_DOWNLOAD_TIMEOUT
        bot_path = f"/bot{quote(self._token, safe=':')}"
        payload = json.dumps({"file_id": file_id}).encode("ascii")
        info = json.loads(self._read_voice_file(f"{bot_path}/getFile", 64 * 1024, deadline, payload))
        result = info.get("result") if isinstance(info, dict) and info.get("ok") is True else None
        path = result.get("file_path") if isinstance(result, dict) else None
        if not isinstance(path, str) or not all(
            _FILE_SEGMENT.fullmatch(segment) and segment not in {".", ".."} for segment in path.split("/")
        ):
            raise ValueError("invalid Telegram file path")
        return Audio(self._read_voice_file(f"/file{bot_path}/{path}", size, deadline), media_type)

    def _transcription_draft(self, message: dict, attachment) -> str:
        if not self._direct_owner(message):
            return "음성 입력은 설정된 개인 채팅에서 소유자가 직접 보낸 메시지만 처리합니다."
        try:
            settings = load_voice_settings(data_dir())
        except (OSError, ValueError):
            return _STT_ERRORS["config"]
        # Disabled voice input gets a short notice and makes no file requests.
        if not settings.enabled or not settings.stt:
            return "음성 입력이 꺼져 있습니다."

        file_id, media_type, size, duration = (
            attachment.get(field) if isinstance(attachment, dict) else None
            for field in ("file_id", "mime_type", "file_size", "duration")
        )
        if not (
            isinstance(file_id, str) and file_id
            and isinstance(media_type, str) and media_type in _VOICE_INPUT_TYPES
            and type(size) is int and 0 < size <= _MAX_VOICE_DOWNLOAD
            and type(duration) is int and 0 <= duration <= _MAX_VOICE_DURATION
        ):
            return "음성 형식, 크기 또는 길이가 허용 범위를 벗어났습니다."
        try:
            provider = voice_for("stt", settings)
        except Exception:
            return _STT_ERRORS["config"]
        try:
            audio = self._download_voice(file_id, size, media_type)
        except TimeoutError:
            return _STT_ERRORS["timeout"]
        except (OSError, http.client.HTTPException, ValueError, RecursionError):
            return "음성 파일을 다운로드하지 못했습니다."
        try:
            draft = provider.transcribe(audio)
        except VoiceSettingsError:
            return _STT_ERRORS["config"]
        except ElevenLabsError as error:
            return _STT_ERRORS.get(error.code, _STT_ERRORS["provider"])
        except TimeoutError:
            return _STT_ERRORS["timeout"]
        except Exception:
            return _STT_ERRORS["provider"]
        if not isinstance(draft, str) or not draft.strip() or self._token in draft:
            return _STT_ERRORS["format"]

        prefix = "전사 초안: "
        suffix = "\n\n질문하려면 이 내용을 텍스트로 보내세요."
        # Leave room for the instructions and an ellipsis in a single Telegram message.
        limit = _CHUNK_LIMIT - len(prefix + suffix) - 1
        units = 0
        for index, char in enumerate(draft):
            units += 2 if ord(char) > 0xFFFF else 1
            if units > limit:
                draft = draft[:index] + "…"
                break
        return prefix + draft + suffix

    def _handle_voice(self, message: dict, attachment, key: str) -> None:
        # Persist the attempt before external calls so a replay never retranscribes audio.
        self._store.save(Record(
            id=None, created_at=self._now().astimezone(UTC).isoformat(), channel="telegram",
            kind="notice", request="", items=(), status="ok", reason="voice_attempted",
            engine=None, model=None, duration_s=None, ref_id=None, update_key=key,
        ))
        self._response("sendMessage", {
            "chat_id": self._chat_id, "text": self._transcription_draft(message, attachment),
            "link_preview_options": {"is_disabled": True},
        })

    def _request(self, method: str, payload: dict):
        backoff = 1
        while True:
            if method == "getUpdates":
                offset = self._fresh_offset()
                if offset is None:
                    payload.pop("offset", None)
                else:
                    payload["offset"] = offset
            status, body = self._response(method, payload)
            api_code = body.get("error_code")
            if type(api_code) is not int or not 400 <= api_code <= 599:
                api_code = 0
            codes = (status, api_code)
            if 300 <= status < 400:
                raise TelegramError(status)
            for fatal in (401, 409):
                if fatal in codes:
                    raise TelegramError(fatal)
            if 429 in codes:
                parameters = body.get("parameters")
                retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
                if type(retry_after) is int and retry_after >= 0:
                    self._sleep(max(1, retry_after))
                    continue
            elif status and all(code < 500 for code in codes):
                code = status if status >= 400 else api_code
                if code:
                    raise TelegramError(code)
                if 200 <= status < 300 and body.get("ok") is True:
                    result = body.get("result")
                    if method == "sendMessage" or isinstance(result, list):
                        return result
            self._sleep(random.uniform(backoff / 2, backoff))
            backoff = min(backoff * 2, 60)

    def _fresh_offset(self) -> int | None:
        if self._cursor is not None:
            offset, timestamp = self._cursor
            if self._now() - timestamp < _CURSOR_LIFETIME:
                return offset
        return None

    def _advance(self, offset: int) -> None:
        current = self._fresh_offset()
        if current is None or offset > current:
            self._store.set_offset(offset)
            self._cursor = (offset, self._now())

    def _resume_deliveries(self) -> set[int]:
        """Drain pending text and return the record IDs completed by this call."""
        delivered = set()
        for record_id, chunks, start in self._store.pending_deliveries():
            record = self._store.get(record_id)
            offset = int(record.update_key.split(":", 1)[0]) + 1
            created = datetime.fromisoformat(record.created_at)
            advance = None
            for index in range(start, len(chunks)):
                last = index == len(chunks) - 1
                text = chunks[index]
                if last and text.endswith(_ID_FOOTER):
                    text = text.removesuffix(str(_DEFERRED_ID)) + str(record_id)
                if self._token in text:
                    text = _SAFE_ERROR
                try:
                    self._request("sendMessage", {
                        "chat_id": self._chat_id, "text": text,
                        "link_preview_options": {"is_disabled": True},
                    })
                except TelegramError as error:
                    if error.code not in (400, 403):
                        raise
                    advance = self._delivery_offset(offset, created)
                    self._store.mark_failed(record_id, f"telegram_{error.code}", offset=advance)
                    break
                advance = self._delivery_offset(offset, created) if last else None
                self._store.mark_sent(record_id, index + 1, offset=advance)
                if last:
                    delivered.add(record_id)
            if advance is not None:
                self._cursor = (advance, self._now())
        return delivered

    def _delivery_offset(self, offset: int, created: datetime) -> int | None:
        # An expired update can still have a reply to send after Telegram resets its IDs.
        if self._now() - created >= _CURSOR_LIFETIME:
            return None
        current = self._fresh_offset()
        return offset if current is None or offset > current else None

    def _deny(self, key: str) -> None:
        self._store.save(Record(
            id=None, created_at=self._now().astimezone(UTC).isoformat(), channel="telegram",
            kind="ask", request="", items=(), status="denied", reason=None, engine=None,
            model=None, duration_s=None, ref_id=None, update_key=key,
        ))

    def _approval_chunks(self, text: str) -> list[str]:
        return [_SAFE_ERROR] if self._token in text else _reply_chunks(text + _ID_FOOTER)

    def _direct_owner(self, message: dict) -> bool:
        sender = message.get("from")
        return (
            isinstance(sender, dict) and type(sender.get("id")) is int
            and sender["id"] == self._chat_id and sender.get("is_bot") is False
            and not any(field.startswith("forward_") for field in message)
            and not message.get("is_automatic_forward")
        )

    def _handle_approval(self, message: dict, words: list[str], key: str) -> None:
        now = self._now()
        if not self._direct_owner(message):
            self._store.save_telegram_approval_reply(key, now, "forbidden", self._approval_chunks(
                "승인 명령은 설정된 개인 채팅에서 소유자가 직접 보낸 메시지만 처리합니다.",
            ))
        elif words == ["승인", "대기"]:
            notes = self._store.telegram_pending_notes(str(self._chat_id), now)
            lines = [f"승인 대기 {len(notes)}건"]
            lines.extend(
                f"메시지 {message_id} · note.create · 1건\n만료 {expires_at}\n승인 ID {id}"
                for message_id, expires_at, id in notes
            )
            lines.append(_PC_ONLY)
            self._store.save_telegram_approval_reply(key, now, "pending", self._approval_chunks("\n\n".join(lines)))
        elif len(words) != 2 or not _APPROVAL_ID.fullmatch(words[1]):
            self._store.save_telegram_approval_reply(key, now, "invalid_arguments", self._approval_chunks(_APPROVAL_USAGE))
        else:
            result = self._approvals.decide(
                words[1].lower(), str(self._chat_id), "telegram", words[0] == "승인", now, key,
                delivery=lambda status: self._approval_chunks(_DECISION_REPLIES[status]),
            )
            claim = result.approval
            if claim is not None:
                try:
                    outcome = self._approvals.execute(claim)
                    if outcome.message is None:
                        self._approvals.finish(claim.id, outcome.ok)
                    else:
                        chunks = _split(outcome.message) if self._token not in outcome.message else [_SAFE_ERROR]
                        self._approvals.finish(claim.id, outcome.ok, chunks=chunks)
                except Exception:
                    # The executor may have published its result. Keep the claim for local recovery.
                    pass
        self._resume_deliveries()

    def _handle_update(self, update) -> None:
        if not isinstance(update, dict):
            return
        update_id = update.get("update_id")
        if type(update_id) is not int or not 0 <= update_id < 2**63 - 1:
            return
        offset = update_id + 1
        message = update.get("message")
        date = message.get("date") if isinstance(message, dict) else None
        if type(date) is not int:
            self._advance(offset)
            return

        key = f"{update_id}:{date}"
        if self._store.by_update_key(key) is not None:
            self._resume_deliveries()
            self._advance(offset)
            return
        chat = message.get("chat")
        private_owner_chat = (isinstance(chat, dict) and chat.get("type") == "private"
                              and type(chat.get("id")) is int and chat["id"] == self._chat_id)
        text = message.get("text")
        words = text.split() if isinstance(text, str) else []
        if not private_owner_chat:
            self._deny(key)
            self._advance(offset)
            return
        if "voice" in message or "audio" in message:
            self._handle_voice(message, message.get("voice", message.get("audio")), key)
            self._advance(offset)
            return
        if not words:
            self._advance(offset)
            return
        if self._token in text:
            self._deny(key)
            self._advance(offset)
            return
        if words[0] in {"승인", "거절"}:
            self._handle_approval(message, words, key)
            return

        message_id = message.get("message_id")

        def submit_note(body: str, now: datetime) -> None:
            self._approvals.propose_telegram_note(body, str(self._chat_id), message_id, key, now, self._approval_chunks)

        can_propose = self._direct_owner(message) and type(message_id) is int and 0 < message_id < 2**63
        staged = _DeferredSave(self._store)
        request = Request("telegram", text, key, submit_note if can_propose else None)
        reply = handle(request, staged, self._engine, self._now())
        if reply.kind == "note":
            self._resume_deliveries()
            return
        record = staged.record
        if reply.kind == "local_only":
            self._store.save(record)
            self._advance(offset)
            return
        fields = [reply.text, record.reason or "", record.model or ""]
        for item in record.items:
            fields.extend((item.text, *item.sources))
        if any(self._token in field for field in fields):
            record = replace(record, items=(), status="error", reason="invalid_output", model=None)
            chunks = [_SAFE_ERROR]
        else:
            chunks = _reply_chunks(reply.text)
        record_id = self._store.save(record, chunks=chunks)
        delivered = self._resume_deliveries()
        if record_id in delivered and record.kind == "ask" and record.status == "ok":
            self._send_voice_reply(record)

    def poll_once(self) -> None:
        self._resume_deliveries()
        payload = {"timeout": _POLL_TIMEOUT, "allowed_updates": ["message"]}
        for update in self._request("getUpdates", payload):
            self._handle_update(update)

    def run(self) -> None:
        while True:
            self.poll_once()
