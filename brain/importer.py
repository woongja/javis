"""Preview and copy approved files from an explicitly selected Markdown directory."""

import hashlib
import os
import stat
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

from core.config import data_dir

from . import Unavailable, _checkout, _is_link, _is_local, _overlaps, _resolve_directory
from . import publish as publisher
from .policy import Rejected, _CONTROL, _allowed_directories, _check_policy, relative_parts
from .publish import (
    VaultRoot,
    _Directory,
    _READ,
    _checked_root,
    _hidden,
    _read_policy,
    _target_directory,
    _target_exists,
    _verify_file,
    pin_configured_root,
    pin_root,
)

MAX_FILES = 2_000
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
_TOO_LARGE = "가져오기 상한을 초과했습니다. 범위를 줄이세요."
_OVERLAP = "원본은 두뇌, 저장소, 일지 폴더와 겹칠 수 없습니다."


def _source_path(src: str | Path) -> Path:
    if src == "":
        raise Rejected("원본 디렉터리를 지정하세요.")
    path = Path.cwd() / Path(src).expanduser()
    if not path.is_absolute() or not _is_local(path):
        raise Rejected("원본은 로컬 디렉터리여야 합니다.")
    if ".." not in path.parts:
        return path
    current = Path(path.anchor)
    for part in path.parts[1:]:
        if part == "..":
            # Check the components that normalization would otherwise discard.
            _resolve_directory(current)
            current = current.parent
        else:
            current /= part
    return current


def _root_id(root: VaultRoot) -> dict:
    return {"realpath": str(root.path), "st_dev": root.device, "st_ino": root.inode}


def _exclusion(name: str, info: os.stat_result) -> str | None:
    if _is_link(info):
        return "link"
    if name.startswith(".") or _hidden(info):
        return "hidden"
    if name.casefold() in _CONTROL:
        return "control"
    if stat.S_ISREG(info.st_mode):
        if Path(name).suffix.casefold() != ".md":
            return "attachment"
    elif not stat.S_ISDIR(info.st_mode):
        return "special"
    try:
        if relative_parts(name) == (name,):
            return None
    except Rejected:
        pass
    return "unsafe_name"


class _SourceChanged(OSError):
    pass


def _source_bytes(directory: _Directory, name: str, root: Path, info: os.stat_result) -> bytes:
    _verify_file(directory, name, root, info)
    fd = os.open(directory.entry(name), _READ | getattr(os, "O_BINARY", 0), dir_fd=directory.fd)
    with os.fdopen(fd, "rb") as source:
        opened = os.fstat(source.fileno())
        _verify_file(directory, name, root, info)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(info, opened):
            raise _SourceChanged("source file changed")
        content = source.read(info.st_size + 1)
        after = os.fstat(source.fileno())
        if len(content) != info.st_size or (after.st_size, after.st_mtime_ns) != (
            info.st_size, info.st_mtime_ns,
        ):
            raise _SourceChanged("source file changed")
        _verify_file(directory, name, root, info)
    return content


def _existing_directory(stack: ExitStack, root: _Directory, parts: tuple[str, ...]) -> _Directory | None:
    try:
        return _target_directory(stack, root, parts, create=False)
    except FileNotFoundError:
        return None


def build_manifest(src: str | Path, dest_rel: str) -> dict:
    """Return a JSON-compatible preview, or raise Rejected/Unavailable with a safe reason.

    Totals include conflicts. Excluded directory entries count once, and
    their descendants are never scanned.
    """
    dest = relative_parts(dest_rel)
    try:
        vault = pin_configured_root()
        source_path = _source_path(src)
        protected = tuple(path for path in (vault.path, _checkout(), data_dir()) if path is not None)
        if any(_overlaps(source_path, path) for path in protected):
            raise Rejected(_OVERLAP)
        actual_source = _resolve_directory(source_path)
        if any(_overlaps(actual_source, path) for path in protected):
            raise Rejected(_OVERLAP)
        source = pin_root(actual_source)
        files = []
        seen_targets: set[str] = set()
        excluded: Counter[str] = Counter()
        total_bytes = 0
        with ExitStack() as roots:
            source_root = _checked_root(roots, source)
            vault_root = _checked_root(roots, vault)
            allowed = _allowed_directories(_read_policy(vault_root))
            if allowed is not None and not any(
                dest[:len(directory)] == directory or directory[:len(dest)] == dest
                for directory in allowed
            ):
                raise Rejected("target is outside the BRAIN.md policy")
            destination = _existing_directory(roots, vault_root, dest)
            pending: list[tuple[str, ...]] = [()]
            while pending:
                parts = pending.pop()
                with ExitStack() as scan:
                    directory = _target_directory(scan, source_root, parts, create=False)
                    target_directory = destination
                    check_directory = destination is not None and bool(parts)
                    scan_path = directory.fd if directory.fd is not None else directory.path
                    with os.scandir(scan_path) as entries:
                        for entry in entries:
                            name = entry.name
                            info = os.lstat(directory.entry(name), dir_fd=directory.fd)
                            if reason := _exclusion(name, info):
                                excluded[reason] += 1
                                continue
                            relative = (*parts, name)
                            if stat.S_ISDIR(info.st_mode):
                                pending.append(relative)
                                continue
                            target = (*dest, *relative)
                            _check_policy(target, allowed)
                            size = info.st_size
                            total_bytes += size
                            if len(files) >= MAX_FILES or total_bytes > MAX_TOTAL_BYTES or size > MAX_FILE_BYTES:
                                raise Rejected(_TOO_LARGE)
                            if check_directory:
                                target_directory = _existing_directory(scan, destination, parts)
                                check_directory = False
                            conflict = target_directory is not None and _target_exists(target_directory, name)
                            target_path = "/".join(target)
                            folded_target = target_path.casefold()
                            conflict = conflict or folded_target in seen_targets
                            seen_targets.add(folded_target)
                            files.append({
                                "src": "/".join(relative),
                                "dest": target_path,
                                "size": size,
                                "sha256": hashlib.sha256(
                                    _source_bytes(directory, name, source.path, info)
                                ).hexdigest(),
                                "status": "conflict" if conflict else "copy",
                            })
    except (Rejected, Unavailable):
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise Rejected("원본 또는 두뇌 경로를 확인하거나 파일을 읽을 수 없습니다.") from error
    files.sort(key=lambda file: file["src"])
    return {
        "source_root": _root_id(source),
        "brain_root": _root_id(vault),
        "dest": "/".join(dest),
        "files": files,
        "totals": {"count": len(files), "bytes": total_bytes},
        "excluded": dict(sorted(excluded.items())),
    }


def _approved_content(source: VaultRoot, parts: tuple[str, ...], file: dict) -> bytes:
    with ExitStack() as stack:
        root = _checked_root(stack, source)
        directory = _target_directory(stack, root, parts[:-1], create=False)
        name = parts[-1]
        info = os.lstat(directory.entry(name), dir_fd=directory.fd)
        if not stat.S_ISREG(info.st_mode) or _exclusion(name, info) or info.st_size != file["size"]:
            raise _SourceChanged("source file changed")
        content = _source_bytes(directory, name, source.path, info)
    if hashlib.sha256(content).hexdigest() != file["sha256"]:
        raise _SourceChanged("source file changed")
    return content


def copy_manifest(manifest: dict) -> tuple[Counter[str], str | None]:
    """Recheck the approved scope and publish only bytes matching the manifest."""
    counts: Counter[str] = Counter()
    copies = []
    destination = manifest["dest"]
    dest_parts = relative_parts(destination)
    for file in manifest["files"]:
        parts = relative_parts(file["src"])
        target = "/".join((*dest_parts, *parts))
        size = file["size"]
        if (
            file["dest"] != target or Path(parts[-1]).suffix.casefold() != ".md"
            or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES
            or not isinstance(file["sha256"], str)
            or file["status"] not in ("conflict", "copy")
        ):
            raise Rejected("invalid import entry")
        if file["status"] == "conflict":
            counts["conflict"] += 1
        else:
            copies.append((file, parts))

    source_path = Path(manifest["source_root"]["realpath"])
    try:
        vault = pin_configured_root()
        protected = (vault.path, _checkout(), data_dir())
        if not _is_local(source_path) or any(
            _overlaps(source_path, path) for path in protected if path is not None
        ):
            raise Rejected(_OVERLAP)
        source = pin_root(source_path)
        if _root_id(vault) != manifest["brain_root"] or _root_id(source) != manifest["source_root"]:
            raise Rejected("approved roots changed")
    except (OSError, RuntimeError, ValueError):
        counts["failed"] += len(copies)
        return counts, "승인한 원본 또는 두뇌 루트를 확인할 수 없습니다."

    try:
        with ExitStack() as stack:
            _allowed_directories(_read_policy(_checked_root(stack, vault)))
    except (OSError, Rejected):
        counts["failed"] += len(copies)
        return counts, "두뇌 루트 또는 BRAIN.md 정책을 확인할 수 없습니다."

    for file, parts in copies:
        try:
            content = _approved_content(source, parts, file)
        except (_SourceChanged, FileNotFoundError, Rejected):
            counts["changed"] += 1
            continue
        except OSError:
            counts["failed"] += 1
            continue
        try:
            status = publisher.publish(vault, file["dest"], content, import_destination=destination).status
        except Exception:
            counts["failed"] += 1
            continue
        counts["failed" if status == "rejected" else status] += 1
    return counts, None
