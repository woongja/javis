"""Shared request, persistence and reply formatting for channel adapters."""

import json
import os
import re
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Literal

import brain
from brain.policy import Policy, Rejected, component_key
from brain.publish import VaultRoot, pin_configured_root, search_policy

from teams import LocalOnly, NoteDraft, Notice, ReplyKind, Team, TeamResult, WebAnswer, registry
from worklog.store import Item, Record, Store

from .approval import ApprovalService, note_proposal_reply, os_user
from .context import Context, build_context
from .engine import Engine, EngineResult, _REFERENCE_LINE
from .recall import recall
from .search import search_reply

_NUMBER_PREFIX = re.compile(r"^(?:\*\*[0-9]{1,2}[.)]\*\*|(\*\*)?[0-9]{1,2}[.)])[ \t]+")
_TITLE_LIMIT = 80
_FAILURES = {
    "timeout": "엔진 제한 시간이 지났습니다.",
    "quota": "엔진 사용 한도에 도달했습니다.",
    "configuration": "Claude CLI 설치와 구독 로그인을 확인해 주세요.",
    "invalid_output": "엔진 응답 형식이 올바르지 않습니다.",
    "output_limit": "엔진 응답이 크기 제한을 초과했습니다.",
}


@dataclass(frozen=True)
class Request:
    channel: str
    text: str
    processing_key: str | None = None
    submit_note: Callable[[str, datetime], None] | None = None


@dataclass(frozen=True)
class Reply:
    text: str
    record_id: int | None
    kind: ReplyKind


def _title_preview(text: str) -> str:
    title = (text.splitlines() or [""])[0]
    if len(title) <= _TITLE_LIMIT:
        return title
    preview = title[:_TITLE_LIMIT - 1].rstrip(" *")
    if preview.count("**") % 2:
        before_bold, _, bold_text = preview.rpartition("**")
        available = _TITLE_LIMIT - len(before_bold) - len("**…**")
        if available > 0:
            return f"{before_bold}**{bold_text[:available].rstrip()}…**"
        return before_bold.rstrip() + "…"
    return preview + "…"


def _render(
    items: tuple[Item, ...], *, sources_only: bool = False, start: int = 1, show_sources: bool = True,
) -> str:
    lines = []
    for number, item in enumerate(items, start):
        text = _NUMBER_PREFIX.sub(r"\1", item.text)
        label = _title_preview(text) if sources_only else text
        lines.append(f"{number}. {label}")
        if show_sources:
            lines.extend(f"   {source}" for source in item.sources or ("기록된 출처 없음",))
    return "\n".join(lines)


@dataclass(frozen=True)
class _Question:
    text: str
    mode: Literal["auto", "web", "private"]


def _team_result(request: Request, teams: dict[str, Team]) -> TeamResult | _Question:
    body = request.text
    team = teams["/web"]
    explicit = body.lstrip().startswith("/")
    if explicit:
        prefix, *arguments = body.split(maxsplit=1)
        if request.channel == "cli" and prefix == "/비공개":
            return _Question(arguments[0], "private") if arguments else Notice("사용법: /비공개 <질문>")
        team = teams.get(prefix)
        if team is None:
            usage = " / ".join(entry.usage for entry in teams.values())
            if request.channel == "cli":
                usage += " / /비공개 <질문>"
            return Notice("사용법: " + usage)
        if team.name == "brain" and request.channel == "telegram":
            return Notice('PC에서 `javis ask "/brain …"`를 실행해 주세요.')
        if not arguments:
            return Notice(f"사용법: {team.usage}")
        if team.name == "note":
            if request.channel != "cli" and request.submit_note is None:
                return Notice("메모 제안은 설정된 개인 채팅에서 소유자가 직접 보낸 메시지만 처리합니다.")
            # Remove one command separator and preserve the rest of the draft as entered.
            body = body.lstrip()[len(prefix) + 1:]
        else:
            body = arguments[0]

    available, reason = team.available()
    if not available:
        return Notice(reason)
    if request.channel == "cli" and team is teams["/web"]:
        return _Question(body, "web" if explicit else "auto")
    return team.handler(body, request.channel)


def _policy_snapshot() -> tuple[VaultRoot, Policy | None]:
    vault = pin_configured_root()
    with os.scandir(vault.path) as entries:
        batch = list(islice(entries, brain.MAX_SCAN_ENTRIES))
    if len(batch) == brain.MAX_SCAN_ENTRIES:
        raise brain.Unavailable("두뇌 정책을 확인할 수 없습니다.")
    return vault, search_policy(vault.path, batch)


def _reference_line(cited: list[str | int], root: Path) -> str:
    # Resolve display names locally, including namesakes outside the selected excerpts.
    names = Counter()
    if any(isinstance(source, str) and "/" in source for source in cited):
        scan = brain._VaultScan()
        names.update(component_key(path.stem) for path, _ in scan.files(root))
        if scan.omitted or scan.read_failures:
            names.clear()
    labels = []
    for source in cited:
        if isinstance(source, int):
            labels.append(f"기록 #{source}")
        else:
            path = PurePosixPath(source)
            name = path.stem if names[component_key(path.stem)] == 1 else path.with_suffix("").as_posix()
            labels.append(f"[[{name}]]")
    return "참고: " + " · ".join(labels)


def _clean_result(result: EngineResult) -> EngineResult:
    if result.status != "ok":
        return result
    items = []
    for item in result.items:
        text = "\n".join(line for line in item.text.splitlines() if not _REFERENCE_LINE.match(line)).strip()
        if text:
            items.append(Item(text, item.sources))
    if not items:
        return replace(result, status="error", reason="invalid_output", items=(), used_ids=())
    return replace(result, items=tuple(items))


def _failure(result: EngineResult) -> str:
    return _FAILURES.get(result.reason, _FAILURES.get(result.status, "엔진 요청에 실패했습니다."))


def _collect_context(question: str, store: Store, web: EngineResult | None) -> tuple[Context, Path] | None:
    if not brain.search_terms(question):
        return None
    try:
        snapshot = _policy_snapshot()
        context = build_context(question, store)
        if not context.sources:
            return None
        if web is not None:
            payload = json.loads(context.payload)
            payload["web_items"] = [{"text": item.text, "sources": item.sources} for item in web.items]
            context = replace(context, payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        if _policy_snapshot() == snapshot:
            return context, snapshot[0].path
    except (brain.Unavailable, Rejected, OSError, sqlite3.Error, UnicodeError):
        pass
    return None


def _cli_answer(question: _Question, store: Store, engine: Engine, record: Record) -> WebAnswer | Reply:
    web = None
    if question.mode == "web":
        web = _clean_result(engine.run(question.text))
        if web.status != "ok":
            return WebAnswer(web)

    root = None
    if question.mode == "private":
        context = Context(json.dumps({"question": question.text, "references": []}, ensure_ascii=False), {})
    else:
        collected = _collect_context(question.text, store, web)
        if collected is None:
            return WebAnswer(web if web is not None else _clean_result(engine.run(question.text)))
        context, root = collected

    run_context = getattr(engine, "run_context", None)
    if callable(run_context):
        result = _clean_result(run_context(context))
    else:
        result = EngineResult("error", "tool_isolation", (), None, 0)
    kind = "private" if question.mode == "private" else "context"
    cited = []
    if result.status == "ok":
        message = _render(result.items, show_sources=False)
        if web is not None:
            # Synthesis can reorder or omit claims. Keep URLs with their original web items.
            sourced = tuple(item for item in web.items if item.sources)
            if sourced:
                message += "\n\n웹 자료\n" + _render(sourced)
        cited = list(dict.fromkeys(context.sources[key] for key in result.used_ids if key in context.sources))
    else:
        message = _failure(result)
    saved = replace(record, kind=kind, status=result.status, duration_s=result.duration_s)
    record_id = store.save(saved, cited_sources=[path for path in cited if isinstance(path, str)])
    message += f"\n\n기록 ID {record_id}"
    if cited and root is not None:
        message += "\n" + _reference_line(cited, root)
    return Reply(message, record_id, kind)


def handle(request: Request, store: Store, engine: Engine, now: datetime | None = None) -> Reply:
    now = now or datetime.now(UTC)
    timestamp = now.astimezone(UTC).isoformat()
    record = Record(
        id=None, created_at=timestamp, channel=request.channel, kind="notice",
        request="", items=(), status="error", reason=None, engine=None, model=None,
        duration_s=None, ref_id=None, update_key=request.processing_key,
    )
    if request.channel == "telegram":
        words = request.text.strip().split(maxsplit=2)
        if words[:2] == ["기록", "검색"]:
            query = words[2] if len(words) == 3 else ""
            status, message = search_reply(store, query, "사용법: 기록 검색 <검색어>")
            saved = replace(record, status="ok") if status in ("ok", "degraded") else record
            return Reply(message, store.save(saved), "notice")

    # Adapters handle their dedicated approval commands before calling this gateway.
    recalled = None if request.text.lstrip().startswith("/") else recall(request.text, store, now)
    if recalled is not None:
        kind: ReplyKind = "notice"
        record = replace(
            record, kind="recall",
            request=request.text, items=recalled.items, status="error" if recalled.reason else "ok",
            reason=recalled.reason, ref_id=recalled.ref_id,
        )
        message = recalled.message or _render(recalled.items, sources_only=True, start=recalled.first_number)
    else:
        answer = _team_result(request, registry(engine))
        if isinstance(answer, _Question):
            answer = _cli_answer(answer, store, engine, record)
            if isinstance(answer, Reply):
                return answer
        if isinstance(answer, WebAnswer):
            kind = "web"
            result = answer.result
            record = replace(
                record, kind="ask", request=request.text, items=result.items, status=result.status,
                reason=result.reason, engine="claude", model=result.model, duration_s=result.duration_s,
            )
            message = (
                _render(result.items) if result.status == "ok"
                else _failure(result)
            )
        elif isinstance(answer, LocalOnly):
            kind = "local_only"
            record = replace(record, kind="local", status=answer.status)
            message = answer.text if request.channel == "cli" else ""
        elif isinstance(answer, NoteDraft):
            if request.submit_note is not None:
                # Telegram persists the approval, record and reply in a single transaction.
                request.submit_note(answer.text, now)
                return Reply("", None, "note")
            draft = ApprovalService(store).propose("note.create", answer.text, os_user(), "cli", now)
            kind = "note"
            record = replace(record, kind="approval", status="ok", reason="proposed")
            message = note_proposal_reply(draft.id, draft.expires_at)
        else:
            kind = "notice"
            message = answer.text

    record_id = store.save(record)
    if kind != "local_only" or request.channel == "cli":
        message = f"{message}\n\n기록 ID {record_id}"
    return Reply(message, record_id, kind)
