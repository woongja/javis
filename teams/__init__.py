"""Fixed team registry and the result types shared by channel adapters."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Literal

import brain

if TYPE_CHECKING:
    from core.engine import Engine, EngineResult


ReplyKind = Literal["web", "local_only", "notice"]


@dataclass(frozen=True)
class WebAnswer:
    result: "EngineResult"


@dataclass(frozen=True)
class LocalOnly:
    text: str
    status: Literal["ok", "error"] = "ok"


@dataclass(frozen=True)
class Notice:
    text: str


TeamResult = WebAnswer | LocalOnly | Notice


@dataclass(frozen=True)
class Team:
    name: str
    description: str
    usage: str
    handler: Callable[[str, str], TeamResult]
    unavailable_reason: str = ""
    availability: Callable[[], tuple[bool, str]] | None = None

    def available(self) -> tuple[bool, str]:
        if self.availability is not None:
            return self.availability()
        return not self.unavailable_reason, self.unavailable_reason


_NOTE_UNAVAILABLE = "메모 기능 준비 중입니다."


def _research(engine: "Engine", text: str, _channel: str) -> WebAnswer:
    return WebAnswer(engine.run(text))


def _placeholder(reason: str, _text: str, _channel: str) -> Notice:
    return Notice(reason)


def _brain_search(text: str, channel: str) -> LocalOnly | Notice:
    if channel != "cli":
        return Notice('PC에서 `javis ask "/brain …"`를 실행해 주세요.')
    try:
        result = brain.search(text)
    except brain.Unavailable as error:
        return Notice(str(error))
    lines = [f"{match.relative_path}\n  {match.excerpt}" for match in result.matches]
    if result.read_failures:
        lines.append(f"읽기 실패 {result.read_failures}건.")
    elif not lines:
        lines.append("검색 범위에서 일치하는 노트가 없습니다.")
    if result.omitted:
        lines.append("검색 한도로 일부 내용이 생략됨.")
    status = "error" if result.read_failures and not result.matches else "ok"
    return LocalOnly("\n\n".join(lines), status)


def registry(engine: "Engine") -> dict[str, Team]:
    return {
        "/web": Team("research", "웹 질의", "/web <질문>", partial(_research, engine)),
        "/brain": Team(
            "brain", "로컬 두뇌 검색", "/brain <검색어>",
            _brain_search, availability=brain.available,
        ),
        "/note": Team(
            "note", "메모 제안", "/note <본문>",
            partial(_placeholder, _NOTE_UNAVAILABLE), _NOTE_UNAVAILABLE,
        ),
    }
