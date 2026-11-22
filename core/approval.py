"""Propose and claim approval work shared by the local and phone gateways."""

import ctypes
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from worklog import Approval, ApprovalKind, Decision, Store
from worklog.store import DecisionStatus


def _note_payload(payload: object) -> str:
    if not isinstance(payload, str) or not payload.strip():
        raise ValueError("note.create requires non-empty text")
    return payload


def _import_payload(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError("brain.import requires a dictionary")
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


_PAYLOAD_VALIDATORS = {"note.create": _note_payload, "brain.import": _import_payload}


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    message: str | None = None


Executor = Callable[[Approval], ExecutionResult | bool]


def note_proposal_reply(id: str, expires_at: str) -> str:
    return f"만료 {expires_at}\n승인 ID {id}\n승인하면 inbox에 새 노트 1개 생성"


class ApprovalService:
    """Gateways decide here, then dispatch a committed claim using their executor registry."""

    def __init__(self, store: Store, executors: Mapping[ApprovalKind, Executor] | None = None):
        self._store = store
        self.executors: dict[ApprovalKind, Executor] = dict(executors) if executors is not None else {}

    def propose(
        self, kind: ApprovalKind, payload: object, owner: str, channel: str, now: datetime | None = None,
    ) -> Approval:
        validator = _PAYLOAD_VALIDATORS.get(kind)
        if validator is None:
            raise ValueError("unsupported approval kind")
        created = datetime.now(UTC) if now is None else now
        if created.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        created = created.astimezone(UTC)
        return self._store.create_approval(
            kind=kind,
            owner=owner,
            allowed_channel=channel,
            created_at=created.isoformat(),
            expires_at=(created + timedelta(hours=24)).isoformat(),
            payload=validator(payload),
        )

    def decide(
        self, id: str, actor: str, channel: str, approve: bool, now: datetime | None = None,
        update_key: str | None = None, *, delivery: Callable[[DecisionStatus], list[str]] | None = None,
    ) -> Decision:
        """Return the stored execution payload only to the caller that claimed it."""
        return self._store.decide_approval(
            id, actor=actor, channel=channel, approve=approve, now=now,
            update_key=update_key, executor=_executor_identity() if approve else None,
            available_kinds=self.executors.keys() if delivery is not None else None, delivery=delivery,
        )

    def propose_telegram_note(
        self, text: str, owner: str, message_id: int, update_key: str, now: datetime,
        reply_chunks: Callable[[str], list[str]],
    ) -> Approval | None:
        """Save a phone note draft and queue a reply containing only approval metadata."""
        def delivery(id: str, expires_at: str) -> list[str]:
            return reply_chunks(f"메시지 {message_id} · note.create · 1건\n{note_proposal_reply(id, expires_at)}")

        return self._store.create_telegram_note_proposal(
            owner=owner, message_id=message_id, update_key=update_key, now=now,
            payload=_note_payload(text), delivery=delivery,
        )

    def execute(self, claim: Approval) -> ExecutionResult:
        result = self.executors[claim.kind](claim)
        return result if isinstance(result, ExecutionResult) else ExecutionResult(result)

    def finish(self, id: str, ok: bool, *, chunks: list[str] | None = None) -> bool:
        """Finalize a claim made by the current process."""
        return self._store.finish_approval(id, ok=ok, executor=_executor_identity(), chunks=chunks)


def os_user() -> str:
    if os.name == "nt":
        from ctypes import wintypes

        get_username = ctypes.WinDLL("advapi32", use_last_error=True).GetUserNameW
        get_username.argtypes = (wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        get_username.restype = wintypes.BOOL
        username = ctypes.create_unicode_buffer(257)  # UNLEN plus terminator
        size = wintypes.DWORD(len(username))
        if not get_username(username, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return username.value

    import pwd

    return pwd.getpwuid(os.geteuid()).pw_name


def _executor_identity() -> tuple[int, str]:
    pid = os.getpid()
    return pid, process_started_at(pid)


def is_interrupted(approval: Approval) -> bool:
    """Require evidence that the recorded executor has exited or its PID was reused."""
    pid = approval.executor_pid
    if approval.status != "executing" or pid is None or pid <= 0 or not approval.executor_started_at:
        return False
    try:
        return process_started_at(pid) != approval.executor_started_at
    except ProcessLookupError:
        return True
    except OSError:
        return False


def process_started_at(pid: int) -> str:
    """Return an OS start time suitable for comparing two sightings of a PID."""
    if sys.platform == "win32":
        return _windows_process_started_at(pid)
    if sys.platform == "darwin":
        return _macos_process_started_at(pid)
    if sys.platform == "linux":
        try:
            stat = Path(f"/proc/{pid}/stat").read_bytes()
        except FileNotFoundError:
            # A hidden or unavailable /proc entry is not proof of exit.
            os.kill(pid, 0)
            raise
        # Field 22 is ticks since boot. The process name in field 2 can contain spaces and parens.
        fields = stat.rsplit(b")", 1)[1].split()
        if fields[0] in (b"Z", b"X"):
            raise ProcessLookupError
        ticks = int(fields[19])
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        return f"{boot_id}:{ticks}"
    raise OSError("process start time is unavailable on this platform")


def _windows_process_started_at(pid: int) -> str:
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (wintypes.LPFILETIME,) * 4
    api.GetProcessTimes.restype = wintypes.BOOL
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL

    process = api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no such PID
            raise ProcessLookupError
        raise ctypes.WinError(error)
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not api.GetProcessTimes(
            process, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if exited.dwHighDateTime or exited.dwLowDateTime:
            raise ProcessLookupError
        return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        api.CloseHandle(process)


def _macos_process_started_at(pid: int) -> str:
    class ProcessInfo(ctypes.Structure):
        # proc_bsdinfo from <sys/proc_info.h>.
        _fields_ = [
            ("identity", ctypes.c_uint32 * 12),
            ("names", ctypes.c_char * 48),
            ("scheduling", ctypes.c_uint32 * 6),
            ("start_seconds", ctypes.c_uint64),
            ("start_microseconds", ctypes.c_uint64),
        ]

    query = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pidinfo
    query.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
    query.restype = ctypes.c_int
    info = ProcessInfo()
    size = ctypes.sizeof(info)
    if query(pid, 3, 0, ctypes.byref(info), size) != size:  # PROC_PIDTBSDINFO
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return f"{info.start_seconds}.{info.start_microseconds:06d}"
