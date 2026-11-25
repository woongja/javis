"""Bounded, read-only search of a configured local Markdown vault."""

import ctypes
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from heapq import nsmallest
from itertools import islice
from pathlib import Path

from core.config import data_dir
from harness import HarnessError
from harness.paths import env_value, repo_root

from .policy import Rejected, relative_parts

MAX_FILE_BYTES = 1024 * 1024
MAX_SCAN_ENTRIES = 5000
MAX_EXCERPT_CHARS = 400
MAX_RESULTS = 5

_SETTING = "JAVIS_BRAIN_DIR"
_INACCESSIBLE = "두뇌 검색 폴더에 접근할 수 없습니다. JAVIS_BRAIN_DIR 설정을 확인하세요."
_HEADING = re.compile(r"^ {0,3}#[ \t]+(.*)$", re.MULTILINE)
_PARAGRAPH_BREAK = re.compile(r"\n[^\S\n]*\n")


class Unavailable(ValueError):
    """The vault configuration cannot be used."""


@dataclass(frozen=True)
class Match:
    relative_path: str
    excerpt: str


@dataclass(frozen=True)
class SearchResult:
    matches: tuple[Match, ...]
    read_failures: int
    omitted: bool


def _checkout() -> Path | None:
    checkout = Path(__file__).resolve().parents[1]
    if (checkout / ".git").exists():
        return checkout
    try:
        return repo_root()
    except HarnessError:
        return None


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _overlaps(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def _is_local(path: Path) -> bool:
    if os.name != "nt":
        return True
    # Reject UNC paths and network-mapped drive letters before accessing the vault.
    return not path.drive.startswith("\\\\") and ctypes.windll.kernel32.GetDriveTypeW(
        ctypes.c_wchar_p(path.anchor)
    ) in (2, 3, 5, 6)  # removable, fixed, optical, RAM disk


def _resolve_directory(path: Path) -> Path:
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise OSError("unsafe directory")
    return path.resolve(strict=True)


def _root() -> Path:
    try:
        checkout = _checkout()
        value = os.environ.get(_SETTING, "").strip()
        if not value and checkout is not None:
            value = env_value(checkout, _SETTING, env={}) or ""
        if not value:
            raise Unavailable("두뇌 검색을 사용하려면 JAVIS_BRAIN_DIR를 설정하세요.")

        configured = Path(value).expanduser()
        configured = Path(os.path.abspath((checkout or Path.cwd()) / configured))
        if not _is_local(configured):
            raise Unavailable("두뇌 검색에는 로컬 폴더를 지정하세요.")
        protected = (data_dir(), checkout) if checkout is not None else (data_dir(),)
        overlap_message = "두뇌 검색 폴더는 저장소 및 일지 폴더와 겹칠 수 없습니다."
        if any(_overlaps(configured, path) for path in protected):
            raise Unavailable(overlap_message)
        root = _resolve_directory(configured)
        if any(_overlaps(root, path) for path in protected):
            raise Unavailable(overlap_message)
        if not root.is_dir():
            raise Unavailable(_INACCESSIBLE)
        return root
    except Unavailable:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise Unavailable(_INACCESSIBLE) from error


def available() -> tuple[bool, str]:
    try:
        with os.scandir(_root()) as entries:
            next(entries, None)
    except Unavailable as error:
        return False, str(error)
    except OSError:
        return False, _INACCESSIBLE
    return True, ""


def _read_markdown(path: Path, info: os.stat_result) -> str | None:
    if info.st_size > MAX_FILE_BYTES:
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with open(path, "rb", opener=lambda name, _: os.open(name, flags)) as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or not os.path.samestat(info, opened):
            raise OSError("vault entry changed")
        if path.resolve(strict=True) != path:
            raise OSError("vault entry was redirected")
        data = source.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        return None
    return data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")


def _matching_passages(content: str, filename: str, terms: list[str]) -> Iterator[tuple[int, int, str]]:
    def matches(text: str) -> bool:
        folded = text.casefold()
        return all(term in folded for term in terms)

    heading = _HEADING.search(content)
    title = re.sub(r"[ \t]+#+[ \t]*$", "", heading[1]).strip() if heading else ""
    if matches(title):
        yield 0, 0, title
    elif matches(filename):
        yield 0, 0, filename
    if heading:
        content = content[:heading.start()] + "\n" + content[heading.end():]
    for index, paragraph in enumerate(_PARAGRAPH_BREAK.split(content)):
        if matches(paragraph):
            yield 1, index, paragraph


def _display(text: str) -> str:
    return "".join(char if char.isprintable() else "?" for char in text)


def search(query: str) -> SearchResult:
    """Rank all query terms within a title or a single paragraph."""
    from .publish import _hidden, search_policy

    terms = query.casefold().split()
    if not terms:
        return SearchResult((), 0, False)
    root = _root()
    read_failures = 0
    omitted = False

    def candidates() -> Iterator[tuple[tuple[int, str, int], str]]:
        nonlocal read_failures, omitted
        directories = [root]
        remaining = MAX_SCAN_ENTRIES
        policy = None
        while directories:
            directory = directories.pop()
            if not remaining:
                omitted = True
                break
            try:
                if _is_link(directory.lstat()) or directory.resolve(strict=True) != directory:
                    raise OSError("vault directory was redirected")
                with os.scandir(directory) as entries:
                    batch = list(islice(entries, remaining + 1))
            except (OSError, RuntimeError):
                read_failures += 1
                continue
            if len(batch) > remaining:
                omitted = True
                if directory == root:
                    # An unseen policy or a second spelling could invalidate the entire search.
                    return
                batch.pop()
            remaining -= len(batch)
            if directory == root:
                try:
                    policy = search_policy(root, batch)
                except (OSError, RuntimeError, Rejected):
                    read_failures += 1
                    return
            children = []
            for entry in sorted(batch, key=lambda entry: entry.name):
                try:
                    if relative_parts(entry.name) != (entry.name,):
                        continue
                except Rejected:
                    continue
                path = directory / entry.name
                relative = path.relative_to(root)
                if policy is not None and policy.excludes(relative.parts):
                    continue
                try:
                    info = path.lstat()
                    if _is_link(info) or _hidden(info):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        children.append(path)
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and path.suffix.casefold() == ".md":
                        content = _read_markdown(path, info)
                        if content is None:
                            omitted = True
                            continue
                        for priority, index, passage in _matching_passages(content, path.stem, terms):
                            yield (priority, relative.as_posix(), index), passage
                except (OSError, RuntimeError, UnicodeError):
                    read_failures += 1
            directories.extend(reversed(children))

    ranked = nsmallest(MAX_RESULTS + 1, candidates(), key=lambda candidate: candidate[0])
    omitted |= len(ranked) > MAX_RESULTS
    matches = []
    for (_, relative, _), passage in ranked[:MAX_RESULTS]:
        excerpt = _display(" ".join(passage.split()))
        if len(excerpt) > MAX_EXCERPT_CHARS:
            omitted = True
            excerpt = excerpt[:MAX_EXCERPT_CHARS - 1] + "…"
        matches.append(Match(_display(relative), excerpt))
    return SearchResult(tuple(matches), read_failures, omitted)
