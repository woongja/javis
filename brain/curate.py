"""Plan and execute approved atomic wiki file changes."""

import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType

from . import MAX_FILE_BYTES, _VaultScan, _is_link
from .context import _metadata
from .importer import _source_bytes
from .policy import Policy, Rejected, _allowed_directories, _check_policy, component_key, relative_parts
from .publish import (
    VaultRoot, _checked_root, _directory, _identity, _policy_bytes, _publish_new, _read_policy,
    _target_directory, _vault_lock, pin_configured_root,
)

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
    with ExitStack() as stack:
        root = _checked_root(stack, vault)
        with _vault_lock(root):
            return _catalog(vault, selected)


def _catalog(vault: VaultRoot, selected: set[str], *, allow_missing: bool = False) -> Catalog:
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
                try:
                    if info.st_size > MAX_FILE_BYTES:
                        raise Rejected("selected note exceeds the read limit")
                    with ExitStack() as parents:
                        parent = _target_directory(parents, root, path.parts[:-1], create=False)
                        data = _source_bytes(parent, path.name, vault.path, info)
                    files[name] = FileSnapshot(data, _identity(info))
                except (OSError, ValueError):
                    if not allow_missing:
                        raise
        if scan.read_failures or (not allow_missing and files.keys() != selected):
            raise Rejected("cannot confirm selected files")
        keys = [component_key(name) for name in paths]
        for name in selected:
            if keys.count(component_key(name)) > 1:
                if not allow_missing:
                    raise Rejected("ambiguous selected file")
                files.pop(name, None)
        policy_hash = "absent"
        raw = _policy_bytes(root)
        policy = None
        if raw is not None:
            policy = _allowed_directories(raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n"))
            policy_hash = hashlib.sha256(raw).hexdigest()
        if policy != scan.policy:
            raise Rejected("BRAIN.md changed")
    return Catalog((vault.device, vault.inode), scan.policy, policy_hash, tuple(paths),
                   not scan.omitted, MappingProxyType(files))


def _wiki_parts(path: str) -> tuple[str, ...]:
    parts = relative_parts(path)
    if ("/".join(parts) != path or not path.endswith(".md")
            or not (path == "javis/wiki/index.md"
                    or len(parts) == 4 and parts[:3] in (("javis", "wiki", "notes"), ("javis", "wiki", "topics")))):
        raise Rejected("invalid wiki destination")
    title = parts[-1][:-3]
    if validated_title(title) != title:
        raise Rejected("wiki destination must be NFC")
    return parts


def _source_paths(sources: dict) -> set[str]:
    paths = set()
    for pin in sources.values():
        if "path" in pin:
            path = pin["path"]
            parts = relative_parts(path)
            if ("/".join(parts) != path or not path.casefold().endswith(".md")
                    or not (parts[0] == "me" or path.startswith(NOTES) and len(parts) == 4)
                    or not re.fullmatch("[0-9a-f]{64}", pin["sha256"])):
                raise Rejected("invalid source pin")
            start, end = pin["byte_range"]
            if type(start) != int or type(end) != int or not 0 <= start < end <= MAX_FILE_BYTES:
                raise Rejected("invalid source range")
            paths.add(path)
        elif "record_id" in pin:
            if type(pin["record_id"]) != int or pin["record_id"] < 1:
                raise Rejected("invalid source record")
        elif pin.get("type") != "conversation" or not isinstance(pin.get("text"), str):
            raise Rejected("invalid conversation source")
    return paths


def _validate_ops(spec: dict, catalog: Catalog) -> None:
    if spec["root_id"] != list(catalog.root_id) or spec["policy_hash"] != catalog.policy_hash:
        raise Rejected("approved vault or policy changed")
    if not catalog.complete:
        raise Rejected("incomplete name inventory")
    targets = {note_link(op["path"]): op for op in spec["ops"]}
    earlier = set()
    names = set()
    phase = 0
    for op in spec["ops"]:
        path = op["path"]
        parts = _wiki_parts(path)
        _check_policy(parts, catalog.policy)
        stage = 0 if path.startswith(NOTES) else 1 if path.startswith(TOPICS) else 2
        dependencies = set(op["depends_on"])
        if stage < phase or not dependencies <= earlier:
            raise Rejected("invalid wiki dependency order")
        phase = stage
        earlier.add(op["id"])
        content = op["content"].encode()
        if not content or len(content) > (MAX_NOTE_BYTES if stage == 0 else 32 * 1024):
            raise Rejected("invalid operation size")
        if not set(op.get("sources", [])) <= spec["sources"].keys():
            raise Rejected("unknown source")
        for link in re.findall(r"\[\[[^\[\]\r\n]+\]\]", op["content"]):
            target = targets.get(link)
            if target and (target["kind"] == "create" or stage > 0) and target["id"] not in dependencies:
                raise Rejected("missing file dependency")
        if op["kind"] == "append":
            continue  # The approval store validated the append preconditions. No adapter yet.
        if op["kind"] != "create" or op.get("absent") is not True:
            raise Rejected("invalid create operation")
        if hashlib.sha256(content).hexdigest() != op["result_sha256"]:
            raise Rejected("invalid result hash")
        key = component_key(parts[-1])
        if key in names:
            raise Rejected("duplicate approved name")
        names.add(key)
        if stage == 0:
            parse_note(content, title=parts[-1][:-3], policy=catalog.policy)


def _file_pins(op: dict, sources: dict) -> dict:
    """Proposal pins also cover existing relation and split targets referenced in the bytes."""
    return {key: pin for key, pin in sources.items() if (path := pin.get("path")) is not None
            and (key in op.get("sources", []) or path == op["path"] or note_link(path) in op["content"])}


def _check_sources(pins: dict, catalog: Catalog, approved: Catalog) -> None:
    for pin in pins.values():
        path = pin["path"]
        file = catalog.selected.get(path)
        if (file is None or file.identity != approved.selected[path].identity
                or pin["byte_range"][1] > len(file.content)
                or hashlib.sha256(file.content).hexdigest() != pin["sha256"]):
            raise Rejected("approved source changed")


def _parent_ids(root, parts: tuple[str, ...]) -> tuple:
    ids = []
    with ExitStack() as stack:
        parent = root
        for part in parts[:-1]:
            matches = parent.matching_names(part)
            if matches and matches != [part]:
                raise Rejected("ambiguous parent")
            try:
                parent = _directory(stack, parent.path / part, parent, inside=True)
            except FileNotFoundError:
                break
            ids.append(_identity(parent.info))
    return tuple(ids)


def _target_snapshot(vault: VaultRoot, path: str) -> FileSnapshot | None:
    parts = _wiki_parts(path)
    with ExitStack() as stack:
        root = _checked_root(stack, vault)
        _check_policy(parts, _allowed_directories(_read_policy(root)))
        if len(_parent_ids(root, parts)) < len(parts) - 1:
            return None
        parent = _target_directory(stack, root, parts[:-1], create=False)
        matches = parent.matching_names(parts[-1])
        try:
            info = os.lstat(parent.entry(parts[-1]), dir_fd=parent.fd)
        except FileNotFoundError:
            if matches:
                raise Rejected("target spelling changed")
            return None
        if (matches != [parts[-1]] or info.st_nlink != 1 or _is_link(info)
                or info.st_size > 32 * 1024):
            raise Rejected("cannot verify wiki target")
        data = _source_bytes(parent, parts[-1], vault.path, info)
        return FileSnapshot(data, _identity(info))


def _create(vault, op, pins, approved, parents):
    def check(parts, root):
        current = _catalog(vault, {pin["path"] for pin in pins.values()})
        if not current.complete or current.policy_hash != approved.policy_hash:
            raise Rejected("wiki policy or inventory changed")
        _check_policy(parts, current.policy)
        if _parent_ids(root, parts)[:len(parents)] != parents:
            raise Rejected("wiki parent changed")
        _check_sources(pins, current, approved)
        if component_key(parts[-1][:-3]) in Names(current.paths, True).used:
            raise FileExistsError

    result = _publish_new(vault, op["path"], op["content"].encode(), check)
    if result.status == "conflict":
        return "conflict"
    if result.status != "published":
        return "failed"
    file = _target_snapshot(vault, op["path"])
    return "done" if file and hashlib.sha256(file.content).hexdigest() == op["result_sha256"] else "failed"


def execute_creates(spec: dict) -> Iterator[tuple[str, str]]:
    """Yield each file outcome so the claim holder can commit it before the next write."""
    ops = spec["ops"]
    invalid = set()
    parents = {}
    pins = {}
    try:
        vault = pin_configured_root()
        if list((vault.device, vault.inode)) != spec["root_id"]:
            raise Rejected("approved vault changed")
        paths = _source_paths(spec["sources"])
        with ExitStack() as stack:
            root = _checked_root(stack, vault)
            stack.enter_context(_vault_lock(root))
            approved = _catalog(vault, paths, allow_missing=True)
            _validate_ops(spec, approved)
            for op in ops:
                key = op["id"]
                pins[key] = _file_pins(op, spec["sources"])
                try:
                    _check_sources(pins[key], approved, approved)
                    parents[key] = _parent_ids(root, _wiki_parts(op["path"]))
                except (OSError, ValueError):
                    invalid.add(key)
            # Deduplicated relations can leave an approved pin with no link in the added bytes.
            used = set().union(*pins.values())
            other = {key: pin for key, pin in spec["sources"].items() if "path" in pin and key not in used}
            _check_sources(other, approved, approved)
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        invalid.update(op["id"] for op in ops)

    done = set()
    for op in ops:
        key = op["id"]
        if not done.issuperset(op["depends_on"]):
            status = "dependency_incomplete"
        elif key in invalid or op["kind"] == "append":
            status = "failed"
        else:
            try:
                status = _create(vault, op, pins[key], approved, parents[key])
            except (OSError, ValueError):
                status = "failed"
        if status == "done":
            done.add(key)
        yield key, status


def reconcile_file(vault: VaultRoot, op: dict, base: str | None, result: str) -> str:
    """Compare only the approved target. Recovery never invokes a publisher."""
    try:
        file = _target_snapshot(vault, op["path"])
        digest = hashlib.sha256(file.content).hexdigest() if file else None
        if digest == result:
            return "done"
        if digest == base and (file is None or list(file.identity) == op.get("file_id")):
            return "unapplied"
    except (OSError, ValueError):
        pass
    return "unknown"
