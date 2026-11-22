"""Shared request, persistence and reply formatting for channel adapters."""

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from teams import LocalOnly, Notice, ReplyKind, Team, TeamResult, WebAnswer, registry
from worklog.store import Item, Record, Store

from .engine import Engine
from .recall import recall

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


@dataclass(frozen=True)
class Reply:
    text: str
    record_id: int
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


def _render(items: tuple[Item, ...], *, sources_only: bool = False, start: int = 1) -> str:
    lines = []
    for number, item in enumerate(items, start):
        text = _NUMBER_PREFIX.sub(r"\1", item.text)
        label = _title_preview(text) if sources_only else text
        lines.append(f"{number}. {label}")
        lines.extend(f"   {source}" for source in item.sources or ("기록된 출처 없음",))
    return "\n".join(lines)


def _team_result(request: Request, teams: dict[str, Team]) -> TeamResult:
    body = request.text
    team = teams["/web"]
    if body.lstrip().startswith("/"):
        prefix, *arguments = body.split(maxsplit=1)
        team = teams.get(prefix)
        if team is None:
            return Notice("사용법: " + " / ".join(entry.usage for entry in teams.values()))
        if team.name == "brain" and request.channel == "telegram":
            return Notice('PC에서 `javis ask "/brain …"`를 실행해 주세요.')
        if not arguments:
            return Notice(f"사용법: {team.usage}")
        body = arguments[0]

    available, reason = team.available()
    if not available:
        return Notice(reason)
    return team.handler(body, request.channel)


def handle(request: Request, store: Store, engine: Engine, now: datetime | None = None) -> Reply:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    # Adapters handle their dedicated approval commands before calling this gateway.
    recalled = None if request.text.lstrip().startswith("/") else recall(request.text, store, now)
    record = Record(
        id=None, created_at=timestamp, channel=request.channel, kind="notice",
        request="", items=(), status="error", reason=None, engine=None, model=None,
        duration_s=None, ref_id=None, update_key=request.processing_key,
    )
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
        if isinstance(answer, WebAnswer):
            kind = "web"
            result = answer.result
            record = replace(
                record, kind="ask", request=request.text, items=result.items, status=result.status,
                reason=result.reason, engine="claude", model=result.model, duration_s=result.duration_s,
            )
            message = (
                _render(result.items) if result.status == "ok"
                else _FAILURES.get(result.reason, _FAILURES.get(result.status, "엔진 요청에 실패했습니다."))
            )
        elif isinstance(answer, LocalOnly):
            kind = "local_only"
            record = replace(record, kind="local", status=answer.status)
            message = answer.text if request.channel == "cli" else ""
        else:
            kind = "notice"
            message = answer.text

    record_id = store.save(record)
    if kind != "local_only" or request.channel == "cli":
        message = f"{message}\n\n기록 ID {record_id}"
    return Reply(message, record_id, kind)
