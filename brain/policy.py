"""Parse the vault's write policy and portable relative paths.

BRAIN.md is UTF-8, with one allowed relative directory per line. Empty
lines are ignored. An empty policy allows no writes. Each directory limits
writes to its descendants, within the inbox or the approved import directory.
"""

import re
from pathlib import PurePosixPath

_RESERVED = {"aux", "con", "conin$", "conout$", "nul", "prn"}
_DEVICES = re.compile(r"(?:com|lpt)[1-9¹²³]")
_SHORT_NAME = re.compile(r"~[0-9]+(?:\.|$)")
_CONTROL = {"brain.md", "agents.md", "claude.md"}
_INBOX = ("javis", "inbox")


class Rejected(ValueError):
    """A path or policy does not permit the requested write."""


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


def check_write(
    path: tuple[str, ...], import_destination: str | None, policy: str | None,
) -> None:
    if PurePosixPath(path[-1]).suffix.casefold() != ".md":
        raise Rejected("target must be a Markdown file")
    base = _INBOX if import_destination is None else relative_parts(import_destination)
    if not _descends_from(path, base):
        raise Rejected("target is outside the approved directory")
    if policy is None:
        return
    try:
        allowed = [relative_parts(line) for line in policy.split("\n") if line]
    except Rejected as error:
        raise Rejected("invalid BRAIN.md policy") from error
    if not any(_descends_from(path, directory) for directory in allowed):
        raise Rejected("target is outside the BRAIN.md policy")
