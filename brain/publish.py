"""Publish approved Markdown bytes to a vault with exclusive file creation."""

import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import uuid4

from . import _is_link, _root
from .policy import Rejected, check_write, relative_parts

_WINDOWS = os.name == "nt"
_READ = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_TEMP = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
_publish_lock = Lock()
_POLICY_NAME = "BRAIN.md"


@dataclass(frozen=True)
class VaultRoot:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class PublishResult:
    status: Literal["published", "conflict", "rejected", "failed"]
    reason: str | None = None


@dataclass(frozen=True)
class _Directory:
    path: Path
    info: os.stat_result
    fd: int | None

    def entry(self, name: str) -> str | Path:
        return name if self.fd is not None else self.path / name

    def matching_names(self, name: str) -> list[str]:
        folded = name.casefold()
        with os.scandir(self.fd if self.fd is not None else self.path) as entries:
            return [entry.name for entry in entries if entry.name.casefold() == folded]


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _hidden(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_HIDDEN)


def _check_realpath(path: Path, root: Path) -> None:
    actual = Path(os.path.realpath(path, strict=True))
    if not actual.is_relative_to(root) or actual.parts != path.parts:
        raise Rejected("vault path was redirected or aliased")


def _directory(
    stack: ExitStack, path: Path, parent: _Directory | None = None, *,
    inside: bool = False, create: bool = False,
) -> _Directory:
    entry = parent.entry(path.name) if parent is not None else path
    dir_fd = parent.fd if parent is not None else None
    try:
        info = os.lstat(entry, dir_fd=dir_fd)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(entry, dir_fd=dir_fd)
        except FileExistsError:
            pass
        info = os.lstat(entry, dir_fd=dir_fd)
    if _is_link(info) or not stat.S_ISDIR(info.st_mode) or (inside and _hidden(info)):
        raise Rejected("unsafe vault directory")
    if _WINDOWS:
        if inside:
            _check_realpath(path, path.parent)
        return _Directory(path, info, None)
    fd = os.open(entry, _READ | os.O_DIRECTORY, dir_fd=dir_fd)
    stack.callback(os.close, fd)
    if not os.path.samestat(info, os.fstat(fd)):
        raise Rejected("vault directory changed")
    return _Directory(path, info, fd)


def _open_root(stack: ExitStack, path: Path) -> _Directory:
    directory = _directory(stack, Path(path.anchor))
    for part in path.parts[1:]:
        directory = _directory(stack, directory.path / part, directory)
    _check_realpath(path, path)
    return directory


def pin_root(path: Path) -> VaultRoot:
    """Capture the configured local vault's identity for later execution.

    Pass an absolute vault path already checked against the checkout and
    worklog locations. Raises ValueError or OSError for an unusable path.
    """
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path.drive.startswith("\\\\"):
        raise Rejected("vault root must be an absolute local path")
    with ExitStack() as stack:
        directory = _open_root(stack, path)
        return VaultRoot(path, *_identity(directory.info))


def pin_configured_root() -> VaultRoot:
    return pin_root(_root())


def _verify_file(
    directory: _Directory, name: str, root: Path, expected: os.stat_result,
    *, allow_hidden: bool = False,
) -> None:
    info = os.lstat(directory.entry(name), dir_fd=directory.fd)
    if (
        _is_link(info) or not stat.S_ISREG(info.st_mode) or not os.path.samestat(info, expected)
        or (not allow_hidden and _hidden(info))
    ):
        raise OSError("vault file changed or is unsafe")
    path = directory.path / name
    try:
        _check_realpath(path, root)
    except Rejected as error:
        raise OSError("vault file was redirected or aliased") from error
    if not os.path.samestat(os.lstat(path), expected):
        raise OSError("vault file changed")


def _read_policy(directory: _Directory) -> str | None:
    try:
        names = directory.matching_names(_POLICY_NAME)
        if not names:
            return None
        if names != [_POLICY_NAME]:
            raise Rejected("ambiguous BRAIN.md policy")
        entry = directory.entry(_POLICY_NAME)
        info = os.lstat(entry, dir_fd=directory.fd)
        if _is_link(info) or not stat.S_ISREG(info.st_mode):
            raise Rejected("unsafe BRAIN.md policy")
        fd = os.open(entry, _READ | getattr(os, "O_BINARY", 0), dir_fd=directory.fd)
        with os.fdopen(fd, encoding="utf-8-sig") as source:
            _verify_file(directory, _POLICY_NAME, directory.path, info, allow_hidden=True)
            if not os.path.samestat(info, os.fstat(source.fileno())):
                raise Rejected("BRAIN.md policy changed")
            return source.read()
    except (OSError, UnicodeError) as error:
        raise Rejected("cannot read or confirm absence of BRAIN.md") from error


def _target_exists(directory: _Directory, name: str) -> bool:
    matches = directory.matching_names(name)
    exists = False
    for spelling in matches or [name]:
        try:
            info = os.lstat(directory.entry(spelling), dir_fd=directory.fd)
        except FileNotFoundError:
            continue
        if _is_link(info) or _hidden(info) or not stat.S_ISREG(info.st_mode):
            raise Rejected("unsafe existing target")
        if _WINDOWS and not matches:
            actual = Path(os.path.realpath(directory.path / name, strict=True))
            if actual.name.casefold() != name.casefold():
                raise Rejected("target is an OS path alias")
        exists = True
    return exists


def _checked_root(stack: ExitStack, vault: VaultRoot) -> _Directory:
    root = _open_root(stack, vault.path)
    if _identity(root.info) != (vault.device, vault.inode):
        raise Rejected("approved vault root changed")
    return root


def _target_parent(
    stack: ExitStack, root: _Directory, parts: tuple[str, ...], *, create: bool,
) -> _Directory:
    directory = root
    for part in parts[:-1]:
        directory = _directory(stack, directory.path / part, directory, inside=True, create=create)
    return directory


def _cleanup_temp(directory: _Directory, name: str, info: os.stat_result) -> None:
    try:
        if _WINDOWS:
            parent = os.lstat(directory.path)
            if _is_link(parent) or not os.path.samestat(parent, directory.info):
                return
        current = os.lstat(directory.entry(name), dir_fd=directory.fd)
        if not _is_link(current) and os.path.samestat(info, current):
            os.unlink(directory.entry(name), dir_fd=directory.fd)
    except FileNotFoundError:
        pass


@contextmanager
def _completed_temp(
    directory: _Directory, root: Path, content: bytes,
) -> Iterator[tuple[str, os.stat_result]]:
    name = f".javis-{uuid4().hex}.tmp"
    fd = os.open(directory.entry(name), _TEMP, 0o600, dir_fd=directory.fd)
    info = None
    try:
        with os.fdopen(fd, "wb") as temp:
            info = os.fstat(temp.fileno())
            _verify_file(directory, name, root, info, allow_hidden=True)
            temp.write(content)
        yield name, info
    finally:
        if info is not None:
            _cleanup_temp(directory, name, info)


def _publish(directory: _Directory, temporary: str, name: str) -> None:
    source = directory.entry(temporary)
    target = directory.entry(name)
    if _WINDOWS:
        os.rename(source, target)
    else:
        os.link(source, target, src_dir_fd=directory.fd, dst_dir_fd=directory.fd, follow_symlinks=False)


def publish(
    vault: VaultRoot, relative_path: str, content: bytes, *,
    import_destination: str | None = None,
) -> PublishResult:
    """Create a Markdown file beneath the pinned vault, creating parent directories.

    Import callers must supply the directory the user approved for this request.
    """
    try:
        parts = relative_parts(relative_path)
        name = parts[-1]
        with ExitStack() as stack:
            root = _checked_root(stack, vault)
            check_write(parts, import_destination, _read_policy(root))
            directory = _target_parent(stack, root, parts, create=True)
            if _target_exists(directory, name):
                return PublishResult("conflict")
            with _completed_temp(directory, vault.path, content) as (temporary, info):
                # Serialize case checks with publication for threads on case-sensitive filesystems.
                with _publish_lock, ExitStack() as verify:
                    verified_root = _checked_root(verify, vault)
                    parent = _target_parent(verify, verified_root, parts, create=False)
                    if not os.path.samestat(directory.info, parent.info):
                        raise Rejected("target directory changed")
                    if _target_exists(parent, name):
                        return PublishResult("conflict")
                    _verify_file(parent, temporary, vault.path, info, allow_hidden=True)
                    # Windows path checks and rename leave a short directory-swap window.
                    try:
                        _publish(parent, temporary, name)
                    except FileExistsError:
                        return PublishResult("conflict")
                try:
                    _verify_file(directory, name, vault.path, info)
                except OSError:
                    return PublishResult("failed", "published file could not be verified")
    except Rejected as error:
        return PublishResult("rejected", str(error))
    except (OSError, UnicodeError):
        return PublishResult("failed", "vault write failed")
    return PublishResult("published")
