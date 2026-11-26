"""Publish approved Markdown bytes to a vault with exclusive file creation."""

import os
import stat
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from . import _is_link, _root
from .policy import (
    MAX_POLICY_BYTES, Policy, Rejected, _allowed_directories, check_write, component_key, relative_parts,
)

_WINDOWS = os.name == "nt"
_READ = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_TEMP = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
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
        folded = component_key(name)
        with os.scandir(self.fd if self.fd is not None else self.path) as entries:
            return [entry.name for entry in entries if component_key(entry.name) == folded]


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _hidden(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_HIDDEN)


def _check_realpath(path: Path, root: Path) -> None:
    actual = Path(os.path.realpath(path, strict=True))
    if not actual.is_relative_to(root) or actual.parts != path.parts:
        raise Rejected("vault path was redirected or aliased")


def _hold_windows_directory(stack: ExitStack, path: Path, expected: os.stat_result) -> None:
    """Keep a path component from being renamed or turned into a junction during publication."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    # READ_ATTRIBUTES, share READ only, OPEN_EXISTING, BACKUP_SEMANTICS | OPEN_REPARSE_POINT.
    handle = api.CreateFileW(str(path), 0x80, 1, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        api.CloseHandle(handle)
        raise
    stack.callback(os.close, fd)
    held = os.fstat(fd)
    if _is_link(held) or not os.path.samestat(held, expected):
        raise Rejected("vault directory changed")


def _directory(
    stack: ExitStack, path: Path, parent: _Directory | None = None, *,
    inside: bool = False, create: bool = False, hold: bool = False,
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
        if hold:
            _hold_windows_directory(stack, path, info)
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


def _policy_bytes(directory: _Directory, names: list[str] | None = None) -> bytes | None:
    try:
        if names is None:
            names = directory.matching_names(_POLICY_NAME)
        if not names:
            return None
        if names != [_POLICY_NAME]:
            raise Rejected("ambiguous BRAIN.md policy")
        entry = directory.entry(_POLICY_NAME)
        info = os.lstat(entry, dir_fd=directory.fd)
        if _is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Rejected("unsafe BRAIN.md policy")
        if info.st_size > MAX_POLICY_BYTES:
            raise Rejected("BRAIN.md policy exceeds 64 KiB")
        fd = os.open(entry, _READ | getattr(os, "O_BINARY", 0), dir_fd=directory.fd)
        with os.fdopen(fd, "rb") as source:
            _verify_file(directory, _POLICY_NAME, directory.path, info, allow_hidden=True)
            if not os.path.samestat(info, os.fstat(source.fileno())):
                raise Rejected("BRAIN.md policy changed")
            data = source.read(MAX_POLICY_BYTES + 1)
            after = os.fstat(fd)
            if (
                len(data) != info.st_size or after.st_size != info.st_size
                or after.st_mtime_ns != info.st_mtime_ns or after.st_nlink != 1
            ):
                raise Rejected("BRAIN.md policy changed")
            _verify_file(directory, _POLICY_NAME, directory.path, info, allow_hidden=True)
            return data
    except (OSError, UnicodeError) as error:
        raise Rejected("cannot read or confirm absence of BRAIN.md") from error


def _read_policy(directory: _Directory, names: list[str] | None = None) -> str | None:
    raw = _policy_bytes(directory, names)
    try:
        return None if raw is None else raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError as error:
        raise Rejected("invalid BRAIN.md encoding") from error


def search_policy(root: Path, entries: Iterable[os.DirEntry]) -> Policy | None:
    """Use the complete, bounded root listing already obtained by search."""
    names = [entry.name for entry in entries if component_key(entry.name) == _POLICY_NAME.casefold()]
    if not names:
        return None
    with ExitStack() as stack:
        return _allowed_directories(_read_policy(_open_root(stack, root), names))


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


def _checked_root(stack: ExitStack, vault: VaultRoot, *, hold: bool = False) -> _Directory:
    root = _open_root(stack, vault.path)
    if _identity(root.info) != (vault.device, vault.inode):
        raise Rejected("approved vault root changed")
    if hold and _WINDOWS:
        _hold_windows_directory(stack, root.path, root.info)
    return root


@contextmanager
def _vault_lock(root: _Directory) -> Iterator[None]:
    """Serialize cooperating publishers across processes without a lock file in the vault."""
    if root.fd is not None:
        import fcntl

        fcntl.flock(root.fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(root.fd, fcntl.LOCK_UN)
        return

    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    api.CreateMutexW.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.ReleaseMutex.argtypes = api.CloseHandle.argtypes = (wintypes.HANDLE,)
    dev, ino = _identity(root.info)
    handle = api.CreateMutexW(None, False, f"Global\\javis-vault-{dev:x}-{ino:x}")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # An abandoned mutex also grants ownership after a publisher exits.
        if api.WaitForSingleObject(handle, 0xFFFFFFFF) not in (0, 0x80):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            api.ReleaseMutex(handle)
    finally:
        api.CloseHandle(handle)


def _target_directory(
    stack: ExitStack, root: _Directory, parts: tuple[str, ...], *, create: bool, hold: bool = False,
) -> _Directory:
    directory = root
    for part in parts:
        directory = _directory(stack, directory.path / part, directory, inside=True, create=create, hold=hold)
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
    def check(parts: tuple[str, ...], root: _Directory) -> None:
        check_write(parts, import_destination, _read_policy(root))

    return _publish_new(vault, relative_path, content, check)


def _publish_new(
    vault: VaultRoot, relative_path: str, content: bytes,
    check: Callable[[tuple[str, ...], _Directory], None],
) -> PublishResult:
    try:
        parts = relative_parts(relative_path)
        name = parts[-1]
        with ExitStack() as stack:
            root = _checked_root(stack, vault)
            stack.enter_context(_vault_lock(root))
            root = _checked_root(stack, vault, hold=True)
            check(parts, root)
            directory = _target_directory(stack, root, parts[:-1], create=True, hold=True)
            if _target_exists(directory, name):
                return PublishResult("conflict")
            with _completed_temp(directory, vault.path, content) as (temporary, info):
                with ExitStack() as verify:
                    verified_root = _checked_root(verify, vault)
                    check(parts, verified_root)
                    parent = _target_directory(verify, verified_root, parts[:-1], create=False)
                    if not os.path.samestat(directory.info, parent.info):
                        raise Rejected("target directory changed")
                    if _target_exists(parent, name):
                        return PublishResult("conflict")
                    _verify_file(parent, temporary, vault.path, info, allow_hidden=True)
                    try:
                        _publish(parent, temporary, name)
                    except FileExistsError:
                        return PublishResult("conflict")
                try:
                    _verify_file(directory, name, vault.path, info)
                except OSError:
                    return PublishResult("failed", "published file could not be verified")
    except FileExistsError:
        return PublishResult("conflict")
    except Rejected as error:
        return PublishResult("rejected", str(error))
    except (OSError, UnicodeError):
        return PublishResult("failed", "vault write failed")
    return PublishResult("published")
