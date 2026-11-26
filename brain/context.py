"""Collect local Markdown evidence for a question in one bounded vault walk."""

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Literal

from . import MAX_FILE_BYTES, _HEADING, _VaultScan, _read_markdown, _root, Unavailable
from .policy import MAX_POLICY_BYTES, Policy, Rejected, component_key, relative_parts

_MAX_BYTES = 16 * 1024 * 1024
_QUESTIONS = frozenset("언제 언제야 어디 어디야 누구 누가 무엇 뭐 뭐야 왜 어떻게 알려줘 알려주세요".split())
_WIKI = ("javis", "wiki")
_LINK = re.compile(r"(!?)\[\[([^\[\]\n]+)\]\]")
_FIELD = re.compile(r"(type|tags|source|derived_sources):(?: (.*))?")
_LIST_ITEM = re.compile(r"""\s*("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[\w-]+)\s*(,|\])""")
_PARAGRAPHS = re.compile(r"\n(?:[ \t]*\n|(?=- (?:사실|관계):))")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_CODE_OR_LINK = re.compile(r"https?://\S+|`+|!?\[\[[^\[\]\n]+\]\]", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Candidate:
    kind: Literal["title", "paragraph", "neighbor"]
    relative_path: str
    excerpt: str
    matched_terms: int
    paragraph: int


def search_terms(question: str) -> tuple[str, ...]:
    """Return distinct normalized terms in question order."""
    terms = []
    for word in re.finditer(r"[^\W_]+", component_key(question)):
        term = word[0][:64]
        if term not in _QUESTIONS and term not in terms:
            terms.append(term)
            if len(terms) == 6:
                break
    return tuple(terms)


def _navigation(parts: tuple[str, ...]) -> bool:
    key = tuple(map(component_key, parts))
    return key[:3] == (*_WIKI, "topics") or key == (*_WIKI, "index.md")


def _scalar(value: str) -> str:
    if value.startswith('"'):
        value = json.loads(value)
    elif re.fullmatch(r"'(?:[^']|'')*'", value):
        value = value[1:-1].replace("''", "'")
    elif not re.fullmatch(r"[\w-]+(?::[\w-]+)?", value):
        raise ValueError
    return value


def _inline_list(value: str) -> list[str]:
    if not value.startswith("[") or not value.endswith("]"):
        raise ValueError
    if not value[1:-1].strip():
        return []
    items = []
    pos = 1
    while pos < len(value):
        item = _LIST_ITEM.match(value, pos)
        if not item or (item[2] == "]") != (item.end() == len(value)):
            raise ValueError
        items.append(_scalar(item[1]))
        pos = item.end()
    return items


def _metadata(header: str) -> dict:
    """Read the four generated fields, rejecting duplicate or ambiguous YAML."""
    fields = {}
    current = None
    indent = ""
    for line in header.split("\n"):
        if not line.strip():
            continue
        if current:
            item = re.fullmatch(r"( *)- (.+)", line)
            if item:
                if fields[current] and indent != item[1]:
                    raise ValueError
                indent = item[1]
                fields[current].append(_scalar(item[2].rstrip()))
                continue
        if current and not fields[current]:
            raise ValueError
        field = _FIELD.fullmatch(line)
        if not field or field[1] in fields:
            raise ValueError
        key, value = field[1], (field[2] or "").strip()
        current = None
        if key in ("tags", "derived_sources"):
            if value:
                fields[key] = _inline_list(value)
            else:
                current = key
                fields[key] = []
        else:
            fields[key] = _scalar(value)
    if current and not fields[current]:
        raise ValueError
    return fields


def _safe_sources(header: str, policy: Policy | None) -> bool:
    try:
        fields = _metadata(header)
        if fields.get("type") != "atomic" or "tags" not in fields:
            return False
        sources = fields["derived_sources"]
        for link in sources:
            match = re.fullmatch(r"\[\[([^\[\]|#\\\n]+)\]\]", link)
            if not match or match[1].casefold().endswith(".md"):
                return False
            parts = relative_parts(match[1])
            file_parts = relative_parts(match[1] + ".md")
            if policy and (policy.excludes(parts) or policy.excludes(file_parts)):
                return False
        source = fields["source"]
        return source in sources or source == "conversation" or bool(re.fullmatch(r"record:[1-9][0-9]*", source))
    except (KeyError, ValueError, RecursionError):
        return False


def _managed(path: Path) -> bool:
    return tuple(map(component_key, path.parts[:3])) == (*_WIKI, "notes")


def _body(content: str, path: Path, policy: Policy | None) -> str | None:
    lines = content.split("\n", 1)
    if lines[0].rstrip() != "---":
        return None if _managed(path) else content
    if len(lines) == 1:
        return None
    end = re.search(r"^(?:---|\.\.\.)[ \t]*$", lines[1], re.MULTILINE)
    if not end:
        return None
    header = lines[1][:end.start()]
    has_sources = _managed(path) or re.search(r"^derived_sources:", header, re.MULTILINE)
    if has_sources and not _safe_sources(header, policy):
        return None
    return lines[1][end.end():].lstrip("\n")


def _target(link: str) -> str:
    name = link.partition("|")[0].partition("#")[0].strip()
    return name[:-3] if name.casefold().endswith(".md") else name


def _prose_lines(body: str) -> Iterator[str]:
    fence = ""
    for line in body.split("\n"):
        marker = _FENCE.match(line)
        if fence:
            if marker and marker[1].startswith(fence) and not marker[2].strip():
                fence = ""
            yield ""
        elif marker:
            fence = marker[1]
            yield ""
        elif line.startswith(("    ", "\t")):
            yield ""
        else:
            yield line


def _relations(body: str) -> Iterator[str]:
    ticks = 0
    for line in _prose_lines(body):
        if not line.strip():
            ticks = 0
            continue
        relation = not ticks and line.startswith("- 관계:")
        for token in _CODE_OR_LINK.finditer(line):
            if token[0].startswith("`"):
                width = len(token[0])
                if not ticks or ticks == width:
                    ticks = 0 if ticks else width
            elif relation and not ticks and token[0].startswith("[["):
                if token.start() and line[token.start() - 1] == "\\":
                    continue
                target = _target(token[0][2:-2])
                try:
                    relative_parts(target + ".md")
                except Rejected:
                    continue
                if target and "\\" not in target:
                    yield target


def _catalog() -> tuple[dict[str, str], dict[str, str]]:
    scan = _VaultScan()
    bodies = {}
    names = {}
    root = _root()
    used = 0
    complete = True
    try:
        for path, info in scan.files(root, skip=_navigation):
            # Policy validation reads at most 64 KiB. Reserve it only if present.
            limit = _MAX_BYTES - (MAX_POLICY_BYTES if scan.policy is not None else 0)
            relative = path.as_posix()
            for key in {component_key(relative[:-3]), component_key(path.stem)}:
                names[key] = None if key in names else relative
            if info.st_size > MAX_FILE_BYTES:
                continue
            if used + info.st_size > limit:
                complete = False
                continue
            content = _read_markdown(root / path, info)
            used += info.st_size
            if content is not None:
                body = _body(content, path, scan.policy)
                if body is not None:
                    bodies[relative] = body
            if used >= limit:
                complete = False
                break
    except (OSError, RuntimeError, UnicodeError) as error:
        raise Unavailable("두뇌 맥락을 읽을 수 없습니다.") from error
    if scan.read_failures:
        raise Unavailable("두뇌 맥락을 읽을 수 없습니다.")
    targets = {
        key: path for key, path in names.items()
        if path in bodies and ("/" in key or (complete and not scan.omitted))
    }
    return bodies, targets


def _excerpt(text: str, targets: dict[str, str]) -> str:
    def label(match: re.Match) -> str:
        target = _target(match[2])
        if match[1] or component_key(target) not in targets:
            return ""
        return match[2].partition("|")[2] or target.rsplit("/", 1)[-1]

    return " ".join(_LINK.sub(label, text).split())


def _title_excerpt(
    body: str, heading: re.Match[str] | None, targets: dict[str, str], count: Callable[[str], int],
) -> str:
    lines = [line.strip() for line in _prose_lines(body)]
    start = body.count("\n", 0, heading.end()) + 1 if heading else 0
    body_lines = lines[start:]
    first = next((line for line in body_lines if line), "")
    summary = [] if first.startswith(("#", "- 사실:", "- 관계:")) else [_excerpt(first, targets)]
    facts = [_excerpt(line, targets) for line in body_lines if line.startswith("- 사실:")]
    prose = "\n".join("" if line.startswith(("#", "- 관계:")) else line for line in lines)
    paragraphs = (
        [_excerpt(line, targets) for line in paragraph.splitlines()]
        for paragraph in _PARAGRAPHS.split(prose)
    )
    # Put the best matching prose paragraph first, breaking ties by note order,
    # so the byte limit favors it when only one excerpt per note is selected.
    best = max(paragraphs, key=lambda paragraph: count(" ".join(paragraph)))
    matched = best if count(" ".join(best)) else []
    return "\n".join(dict.fromkeys(line for line in (*matched, *summary, *facts) if line))


def _first_fact(path: str, body: str, targets: dict[str, str]) -> Candidate | None:
    prose = _HEADING.sub("", "\n".join(_prose_lines(body)), count=1)
    first = None
    for index, paragraph in enumerate(_PARAGRAPHS.split(prose)):
        raw = paragraph.strip()
        if raw.startswith("- 사실:"):
            excerpt = _excerpt(raw.split("\n", 1)[0], targets)
            if excerpt.removeprefix("- 사실:").strip():
                return Candidate("neighbor", path, excerpt, 0, index)
        elif first is None and raw and not raw.startswith(("#", "- 관계:")):
            excerpt = _excerpt(raw, targets)
            if excerpt:
                first = Candidate("neighbor", path, excerpt, 0, index)
    return None if _managed(Path(path)) else first


def context_candidates(question: str) -> list[Candidate]:
    """Return direct passages and one-hop facts, or raise Unavailable on scan failure."""
    terms = search_terms(question)
    if not terms:
        return []
    bodies, targets = _catalog()

    def count(text: str) -> int:
        folded = component_key(text)
        return sum(term in folded for term in terms)

    titles, paragraphs = [], []
    parents = {}
    for path, body in sorted(bodies.items()):
        heading = _HEADING.search(body)
        filename = _excerpt(Path(path).stem, targets)
        title = filename
        if heading:
            label = _excerpt(re.sub(r"(?<=[ \t])#+$", "", heading[1].rstrip()), targets)
            title = max((label, filename), key=count)
        matches = count(title)
        if matches:
            excerpt = _title_excerpt(body, heading, targets, count) or title
            titles.append(Candidate("title", path, excerpt, matches, 0))
            parents[path] = (-matches, 0, path)
        if heading:
            body = body[:heading.start()] + "\n" + body[heading.end():]
        for index, paragraph in enumerate(_PARAGRAPHS.split(body)):
            excerpt = _excerpt(paragraph, targets)
            if not excerpt:
                continue
            matches = count(excerpt)
            if matches:
                paragraphs.append(Candidate("paragraph", path, excerpt, matches, index))
                key = (-matches, 1, path)
                parents[path] = min(key, parents.get(path, key))

    neighbors = []
    seen = set()
    for parent in sorted(parents, key=parents.__getitem__)[:5]:
        for target in islice(_relations(bodies[parent]), 3):
            path = targets.get(component_key(target))
            if path is None or path == parent or path in seen:
                continue
            seen.add(path)
            if fact := _first_fact(path, bodies[path], targets):
                neighbors.append(fact)
    return titles + paragraphs + neighbors
