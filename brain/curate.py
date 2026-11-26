"""Atomic wiki notes and the read-only vault snapshot used to plan changes."""

import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType

from . import MAX_FILE_BYTES, _VaultScan
from .context import _metadata
from .importer import _source_bytes
from .policy import MAX_POLICY_BYTES, Policy, Rejected, _allowed_directories, component_key, relative_parts
from .publish import VaultRoot, _checked_root, _target_directory

NOTES = "javis/wiki/notes/"
TOPICS = "javis/wiki/topics/"
MAX_NOTE_BYTES = 2048


def validated_title(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a title")
    value = unicodedata.normalize("NFC", value)
    if not value or value != value.strip() or any(
        not (unicodedata.category(c).startswith("L") or unicodedata.category(c) == "Nd" or c in " -_")
        for c in value
    ):
        raise ValueError("invalid note title")
    relative_parts(value + ".md")
    if len((value + ".md").encode()) > 120:
        raise ValueError("접미사를 포함해 더 짧은 제목이 필요합니다.")
    return value


class Names:
    def __init__(self, paths: tuple[str, ...], complete: bool):
        self.used = {component_key(PurePosixPath(path).stem) for path in paths}
        self.complete = complete

    def allocate(self, title: str) -> str:
        if not self.complete:
            raise ValueError("이름 탐색이 잘려 생성을 보류합니다.")
        base = candidate = validated_title(title)
        suffix = 2
        while component_key(candidate) in self.used:
            candidate = validated_title(f"{base}-{suffix}")
            suffix += 1
        self.used.add(component_key(candidate))
        return candidate


def line(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise ValueError("expected one nonempty printable line")
    return value


def note_link(path: str) -> str:
    relative_parts(path)
    if not path.casefold().endswith(".md") or any(c in path for c in r"\[]#|^") or not path.isprintable():
        raise ValueError("invalid note link")
    stem = path[:-3]
    relative_parts(stem)
    if stem.casefold().endswith(".md"):
        raise ValueError("ambiguous note link")
    return f"[[{stem}]]"


def link_path(link: str) -> str:
    if not isinstance(link, str) or not link.startswith("[[") or not link.endswith("]]"):
        raise ValueError("invalid source link")
    path = link[2:-2] + ".md"
    if note_link(path) != link:
        raise ValueError("invalid source link")
    return path


@dataclass(frozen=True)
class AtomicNote:
    title: str
    summary: str
    facts: tuple[str, ...]
    relations: tuple[str, ...]  # Label followed by a wikilink.
    tags: tuple[str, ...]
    source: str
    derived_sources: tuple[str, ...]

    def validate(self, policy: Policy | None = None) -> None:
        validated_title(self.title)
        for items in (self.tags, self.facts, self.relations, self.derived_sources):
            if not isinstance(items, (list, tuple)):
                raise ValueError("expected a list of strings")
        line(self.source)
        if not 1 <= len(self.facts) <= 5 or len(self.relations) > 5:
            raise ValueError("atomic note exceeds fact or relation limits")
        for value in (self.summary, *self.tags, *self.facts, *self.relations):
            line(value)
        for value in self.relations:
            if not re.fullmatch(r"(?:[^\[\]]+ )?\[\[[^\[\]|#]+\]\]", value):
                raise ValueError("expected a relation to one note")
            link_path(value[value.index("[["):])
        if (self.source != "conversation" and not re.fullmatch(r"record:[1-9][0-9]*", self.source)
                and self.source not in self.derived_sources):
            raise ValueError("missing direct source")
        for link in self.derived_sources:
            path = link_path(link)
            if policy and (policy.excludes(relative_parts(path)) or policy.excludes(relative_parts(path[:-3]))):
                raise Rejected("excluded source")


def derived_sources(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({link for group in groups for link in group}, key=link_path))


def serialize_note(note: AtomicNote, *, policy: Policy | None = None) -> bytes:
    note.validate(policy)

    def quoted(value: object) -> str:
        return json.dumps(value, ensure_ascii=False)

    sources = derived_sources(note.derived_sources)
    lines = ["---", "type: atomic", "tags: " + quoted(note.tags), "source: " + quoted(note.source)]
    lines.append("derived_sources:" if sources else "derived_sources: []")
    lines.extend("  - " + quoted(link) for link in sources)
    lines.extend(("---", "# " + validated_title(note.title), note.summary, ""))
    lines.extend("- 사실: " + fact for fact in note.facts)
    lines.extend("- 관계: " + relation for relation in note.relations)
    data = ("\n".join(lines) + "\n").encode()
    if len(data) > MAX_NOTE_BYTES:
        raise ValueError("atomic note exceeds 2048 bytes")
    return data


def parse_note(data: bytes, *, title: str | None = None, policy: Policy | None = None) -> AtomicNote:
    if len(data) > MAX_NOTE_BYTES:
        raise ValueError("atomic note exceeds 2048 bytes")
    lines = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0] != "---":
        raise ValueError("missing atomic frontmatter")
    end = lines.index("---", 1)
    fields = _metadata("\n".join(lines[1:end]))
    if set(fields) != {"type", "tags", "source", "derived_sources"} or fields["type"] != "atomic":
        raise ValueError("invalid atomic frontmatter")
    body = lines[end + 1:]
    if len(body) < 3 or not body[0].startswith("# "):
        raise ValueError("missing atomic note body")
    heading = body[0][2:]
    if title is not None and heading != title:
        raise ValueError("heading differs from file name")
    facts, relations = [], []
    for item in body[2:]:
        if item.startswith("- 사실: "):
            facts.append(item[6:])
        elif item.startswith("- 관계: "):
            relations.append(item[6:])
        elif item.strip():
            raise ValueError("invalid atomic note item")
    note = AtomicNote(heading, body[1], tuple(facts), tuple(relations), tuple(fields["tags"]),
                      fields["source"], tuple(fields["derived_sources"]))
    note.validate(policy)
    return note


@dataclass(frozen=True)
class FileSnapshot:
    content: bytes
    identity: tuple[int, int]


@dataclass(frozen=True)
class Catalog:
    root_id: tuple[int, int]
    policy: Policy | None
    policy_hash: str
    paths: tuple[str, ...]
    complete: bool
    selected: Mapping[str, FileSnapshot]


def read_catalog(vault: VaultRoot, selected: set[str]) -> Catalog:
    """Inventory allowed names, opening only the policy and explicitly selected files."""
    if len(selected) > 6:
        raise Rejected("select at most six reference files")
    scan = _VaultScan()
    files = {}
    paths = []
    with ExitStack() as stack:
        root = _checked_root(stack, vault)
        for path, info in scan.files(vault.path, skip=lambda _: False):
            name = path.as_posix()
            paths.append(name)
            if name in selected:
                if info.st_size > MAX_FILE_BYTES:
                    raise Rejected("selected note exceeds the read limit")
                with ExitStack() as parents:
                    parent = _target_directory(parents, root, path.parts[:-1], create=False)
                    data = _source_bytes(parent, path.name, vault.path, info)
                files[name] = FileSnapshot(data, (info.st_dev, info.st_ino))
        if scan.read_failures or files.keys() != selected:
            raise Rejected("cannot confirm selected files")
        keys = [component_key(name) for name in paths]
        if any(keys.count(component_key(name)) != 1 for name in selected):
            raise Rejected("ambiguous selected file")
        policy_hash = "absent"
        if scan.policy is not None:
            info = os.lstat(root.entry("BRAIN.md"), dir_fd=root.fd)
            if info.st_size > MAX_POLICY_BYTES or info.st_nlink != 1:
                raise Rejected("invalid BRAIN.md")
            raw = _source_bytes(root, "BRAIN.md", vault.path, info)
            policy = _allowed_directories(raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n"))
            if policy != scan.policy:
                raise Rejected("BRAIN.md changed")
            policy_hash = hashlib.sha256(raw).hexdigest()
    return Catalog((vault.device, vault.inode), scan.policy, policy_hash, tuple(paths),
                   not scan.omitted, MappingProxyType(files))
