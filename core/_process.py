"""Bounded UTF-8 subprocess transport with process-tree cleanup."""

import errno
import os
import signal
import subprocess
import time
from io import BufferedReader, BufferedWriter
from pathlib import Path
from queue import Empty, Queue
from threading import Thread

OUTPUT_LIMIT = 1024 * 1024  # Per output stream, in bytes.
_CREATE_SUSPENDED = 0x00000004


class OutputLimitExceeded(Exception):
    pass


class _WindowsJob:
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("thread_id", wintypes.DWORD),
                ("owner_pid", wintypes.DWORD), ("base_priority", wintypes.LONG),
                ("delta_priority", wintypes.LONG), ("flags", wintypes.DWORD),
            ]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.thread_entry = ThreadEntry
        self.invalid_handle = ctypes.c_void_p(-1).value
        for name, result, arguments in (
            ("CreateJobObjectW", wintypes.HANDLE, (wintypes.LPVOID, wintypes.LPCWSTR)),
            ("OpenProcess", wintypes.HANDLE, (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)),
            ("AssignProcessToJobObject", wintypes.BOOL, (wintypes.HANDLE, wintypes.HANDLE)),
            ("TerminateJobObject", wintypes.BOOL, (wintypes.HANDLE, wintypes.UINT)),
            ("CloseHandle", wintypes.BOOL, (wintypes.HANDLE,)),
            ("CreateToolhelp32Snapshot", wintypes.HANDLE, (wintypes.DWORD, wintypes.DWORD)),
            ("Thread32First", wintypes.BOOL, (wintypes.HANDLE, ctypes.POINTER(ThreadEntry))),
            ("Thread32Next", wintypes.BOOL, (wintypes.HANDLE, ctypes.POINTER(ThreadEntry))),
            ("OpenThread", wintypes.HANDLE, (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)),
            ("ResumeThread", wintypes.DWORD, (wintypes.HANDLE,)),
        ):
            function = getattr(self.api, name)
            function.restype = result
            function.argtypes = arguments
        self.error = ctypes.WinError
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise self.error(ctypes.get_last_error())

    def assign(self, pid: int) -> None:
        from ctypes import get_last_error

        process = self.api.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not process:
            raise self.error(get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle, process):
                raise self.error(get_last_error())
        finally:
            self.api.CloseHandle(process)

    def resume(self, pid: int) -> None:
        from ctypes import byref, get_last_error, sizeof

        # Popen closes the primary thread handle. The suspended process has exactly one thread.
        snapshot = self.api.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == self.invalid_handle:
            raise self.error(get_last_error())
        try:
            entry = self.thread_entry()
            entry.size = sizeof(entry)
            found = self.api.Thread32First(snapshot, byref(entry))
            while found and entry.owner_pid != pid:
                found = self.api.Thread32Next(snapshot, byref(entry))
            if not found:
                raise self.error(get_last_error())
            thread = self.api.OpenThread(0x0002, False, entry.thread_id)  # THREAD_SUSPEND_RESUME
            if not thread:
                raise self.error(get_last_error())
            try:
                if self.api.ResumeThread(thread) == 0xFFFFFFFF:
                    raise self.error(get_last_error())
            finally:
                self.api.CloseHandle(thread)
        finally:
            self.api.CloseHandle(snapshot)

    def close(self) -> None:
        self.api.TerminateJobObject(self.handle, 1)
        self.api.CloseHandle(self.handle)


def _read(pipe: BufferedReader, output: bytearray) -> None:
    while chunk := pipe.read1(65536):
        if len(output) + len(chunk) > OUTPUT_LIMIT:
            raise OutputLimitExceeded
        output.extend(chunk)


def _write(pipe: BufferedWriter, payload: bytes) -> None:
    try:
        with pipe:
            pipe.write(payload)
    except BrokenPipeError:
        pass  # The CLI can report an error before consuming stdin.
    except OSError as exc:
        if os.name != "nt" or exc.errno != errno.EINVAL:
            raise


def run_bounded(
    args: list[str], payload: bytes, cwd: Path, env: dict[str, str], timeout: float,
) -> tuple[int, bytes, bytes]:
    deadline = time.monotonic() + timeout
    job = _WindowsJob() if os.name == "nt" else None
    process = None
    threads = []
    completed: Queue[Exception | None] = Queue()
    stdout, stderr = bytearray(), bytearray()

    def transfer(function, *arguments):
        try:
            function(*arguments)
        except Exception as exc:
            completed.put(exc)
        else:
            completed.put(None)

    try:
        process = subprocess.Popen(
            args, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=job is None,
            creationflags=_CREATE_SUSPENDED if job is not None else 0,
        )
        if job is not None:
            job.assign(process.pid)
            job.resume(process.pid)
        for function, pipe, value in (
            (_read, process.stdout, stdout), (_read, process.stderr, stderr),
            (_write, process.stdin, payload),
        ):
            thread = Thread(target=transfer, args=(function, pipe, value))
            thread.start()
            threads.append(thread)
        for _ in threads:
            remaining = deadline - time.monotonic()
            try:
                failure = completed.get(timeout=max(0, remaining))
            except Empty:
                raise subprocess.TimeoutExpired(args, timeout) from None
            if failure is not None:
                raise failure
        return process.wait(timeout=max(0, deadline - time.monotonic())), bytes(stdout), bytes(stderr)
    finally:
        if job is not None:
            job.close()
        if process is not None:
            if job is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                # Also reap the direct child if assignment failed.
                process.kill()
            process.wait()
            for thread in threads:
                thread.join()
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()
