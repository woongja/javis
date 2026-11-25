"""Portable relative paths and BRAIN.md read exclusions and write permissions."""

import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

_RESERVED = {"aux", "con", "conin$", "conout$", "nul", "prn"}
_DEVICES = re.compile(r"(?:com|lpt)[1-9¹²³]")
_SHORT_NAME = re.compile(r"~[0-9]+(?:\.|$)")
_CONTROL = {"brain.md", "agents.md", "claude.md"}
_INBOX = ("javis", "inbox")
MAX_POLICY_BYTES = 64 * 1024


class Rejected(ValueError):
    """A path or policy does not permit the requested operation."""


def relative_parts(path: str) -> tuple[str, ...]:
    parts = tuple(re.split(r"[/\\]", path))
    for part in parts:
        if (
            not part or part.startswith(".") or part.endswith((" ", "."))
            or any(ord(char) < 32 or ord(char) == 127 or char in '<>:"|?*' for char in part)
            or _SHORT_NAME.search(part)
        ):
            raise Rejected("invalid relative path")
        folded = part.casefold()
        stem = folded.partition(".")[0].rstrip(" ")
        if stem in _RESERVED or _DEVICES.fullmatch(stem) or folded in _CONTROL:
            raise Rejected("reserved or control path")
    return parts


def _descends_from(path: tuple[str, ...], directory: tuple[str, ...]) -> bool:
    return len(path) > len(directory) and path[:len(directory)] == directory


def component_key(name: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


@dataclass(frozen=True)
class Policy:
    allowed: tuple[tuple[str, ...], ...]
    excluded: frozenset[tuple[str, ...]]

    def __iter__(self) -> Iterator[tuple[str, ...]]:
        # Import previews iterate the write grants to check the proposed destination.
        return iter(self.allowed)

    def excludes(self, path: tuple[str, ...]) -> bool:
        if not self.excluded:
            return False
        key = tuple(map(component_key, path))
        return any(key[:end] in self.excluded for end in range(1, len(key) + 1))


def _allowed_directories(policy: str | None) -> Policy | None:
    if policy is None:
        return None
    try:
        allowed = []
        excluded = set()
        for line in policy.split("\n"):
            if line.startswith("exclude: "):
                parts = relative_parts(line.removeprefix("exclude: "))
                excluded.add(tuple(map(component_key, parts)))
            elif line:
                allowed.append(relative_parts(line))
        return Policy(tuple(allowed), frozenset(excluded))
    except Rejected as error:
        raise Rejected("invalid BRAIN.md policy") from error


def _check_policy(path: tuple[str, ...], policy: Policy | None) -> None:
    if policy is not None and (
        policy.excludes(path) or not any(_descends_from(path, directory) for directory in policy.allowed)
    ):
        raise Rejected("target is outside the BRAIN.md policy")


def check_write(
    path: tuple[str, ...], import_destination: str | None, policy: str | None,
) -> None:
    if PurePosixPath(path[-1]).suffix.casefold() != ".md":
        raise Rejected("target must be a Markdown file")
    base = _INBOX if import_destination is None else relative_parts(import_destination)
    if not _descends_from(path, base):
        raise Rejected("target is outside the approved directory")
    _check_policy(path, _allowed_directories(policy))
