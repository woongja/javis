"""One approved append per local connection. The adapter owns the listener and boot key.

v1 frames are UTF-8 JSON (one Windows pipe message or a 4-byte big-endian
length and bytes on AF_UNIX). The adapter generates a new 32-byte random key
at startup and publishes its registration in the local javis data directory.
Windows wraps it with current-user DPAPI. POSIX requires a 0700 directory.
Each hello and challenge has a fresh nonce. Both proofs cover the two nonces,
vault, approval and operation IDs. A preview ends after the adapter proof.
The hello carries version 1, client nonce, vault [device, inode] as decimal
strings, approval ID and op ID. The challenge returns nonce and adapter proof.
HMAC-SHA256 input is a compact JSON array of strings in _proof's field order.
The request uses the executor proof as its session token and standard base64
for the exact UTF-8 addition. An authenticated response echoes session and
returns status ok, conflict or rejected. Before authentication a rejection
contains status only. No append is retried after a missing response.
The adapter must load the executing claim from its own store and bind it to
the kernel-reported peer PID. It processes exactly one request, then closes.
The Vault.process callback must recheck the path, policy, pins and bytes.
"""

import base64
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import stat
import struct
import sys
from contextlib import ExitStack, contextmanager
from multiprocessing import connection

from . import curate
from .policy import Rejected

LIMIT = 64 * 1024
TIMEOUT = 3


class Conflict(ValueError):
    """The approved base has changed."""


class Disconnected(OSError):
    """No authenticated adapter accepted the session."""


def verified_append(base: bytes, op: dict, evidence, *, policy=None) -> bytes:
    """Return the exact prospective bytes, or raise Conflict/ValueError before any write."""
    path = op["path"]
    parts = curate._wiki_parts(path)
    if op["kind"] != "append":
        raise Rejected("expected an append")
    if hashlib.sha256(base).hexdigest() != op["base_sha256"]:
        raise Conflict("base bytes changed")
    addition = op["content"].encode("utf-8")
    result = base + addition
    if hashlib.sha256(result).hexdigest() != op["result_sha256"]:
        raise Rejected("result hash differs")
    atomic = path.startswith(curate.NOTES)
    if len(result) > (curate.MAX_NOTE_BYTES if atomic else 32 * 1024):
        raise Rejected("result exceeds file limit")
    base.decode("utf-8-sig")
    match = re.search(rb"\r\n|[\r\n]", base)
    newline = match[0] if match else b"\n"
    prefix = newline if base and not base.endswith((b"\r", b"\n")) else b""
    if not addition.startswith(prefix) or not addition.endswith(newline):
        raise Rejected("expected complete appended lines")
    lines = addition[len(prefix):-len(newline)].decode().split(newline.decode())
    for line in lines:
        if not line.isprintable() or not line:
            raise Rejected("invalid appended line")
        if atomic:
            if not line.startswith(("- 사실: ", "- 관계: ")):
                raise Rejected("expected a fact or relation")
        else:
            if not line.startswith("- [["):
                raise Rejected("expected an index link")
            target = curate.link_path(line[2:])
            curate._wiki_parts(target)
    if atomic:
        current = curate.parse_note(base, title=parts[-1][:-3], policy=policy)
        if not set(evidence) <= set(current.derived_sources):
            raise Rejected("new evidence needs a separate note")
        curate.parse_note(result, title=current.title, policy=policy)
    return result


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.c_void_p)]

    source = Blob(len(data), ctypes.cast(ctypes.c_char_p(data), ctypes.c_void_p))
    output = Blob()
    api = ctypes.WinDLL("crypt32", use_last_error=True)
    operation = api.CryptProtectData if protect else api.CryptUnprotectData
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(output.data))


def _private_directory(directory):
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or (os.name != "nt" and
            (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
        raise Rejected("expected a private IPC directory")


def registration(key):
    """Bytes the desktop adapter publishes at obsidian-append.key on startup."""
    if len(key) != 32:
        raise ValueError("expected a 256-bit adapter key")
    return _dpapi(key, protect=True) if os.name == "nt" else key


def _boot_key(directory):
    _private_directory(directory)
    path = directory / "obsidian-append.key"
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            os.name != "nt" and info.st_uid != os.geteuid()):
        raise Rejected("invalid adapter registration file")
    with path.open("rb") as source:
        if not os.path.samestat(info, os.fstat(source.fileno())):
            raise Rejected("adapter registration changed")
        data = source.read(4097)
    if len(data) > 4096:
        raise Rejected("invalid adapter registration")
    key = _dpapi(data, protect=False) if os.name == "nt" else data
    if len(key) != 32:
        raise Rejected("invalid adapter key")
    return key


def address(directory):
    _private_directory(directory)
    if os.name == "nt":
        name = hashlib.sha256(os.fsencode(directory)).hexdigest()
        return rf"\\.\pipe\javis-append-{name}"
    return str(directory / "append.sock")


def _user_pipe(endpoint):
    """Create the first local-only message pipe with a protected current-user DACL."""
    import _winapi
    from ctypes import wintypes

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    token_user = ctypes.create_string_buffer(256)  # TOKEN_USER including SECURITY_MAX_SID_SIZE
    length = wintypes.DWORD()
    if not api.GetTokenInformation(ctypes.c_void_p(-4), 1, token_user, len(token_user), ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    sid = wintypes.LPWSTR()
    if not api.ConvertSidToStringSidW(ctypes.c_void_p.from_buffer(token_user), ctypes.byref(sid)):
        raise ctypes.WinError(ctypes.get_last_error())
    with ExitStack() as stack:
        stack.callback(ctypes.windll.kernel32.LocalFree, sid)
        descriptor = ctypes.c_void_p()
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"D:P(A;;GA;;;{sid.value})", 1, ctypes.byref(descriptor), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        stack.callback(ctypes.windll.kernel32.LocalFree, descriptor)

        class Attributes(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]

        attrs = Attributes(ctypes.sizeof(Attributes), descriptor, False)
        return _winapi.CreateNamedPipe(
            endpoint, _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE | _winapi.FILE_FLAG_OVERLAPPED,
            _winapi.PIPE_TYPE_MESSAGE | _winapi.PIPE_READMODE_MESSAGE | 0x08, 1,
            LIMIT, LIMIT, TIMEOUT * 1000, ctypes.addressof(attrs),
        )


@contextmanager
def listen_once(directory):
    """Listen for one local session. Caller closes the accepted connection after serve_session."""
    endpoint = address(directory)
    if os.name != "nt":
        with connection.Listener(endpoint, family="AF_UNIX") as listener:
            yield listener.accept
        return
    import _winapi

    with connection.PipeConnection(_user_pipe(endpoint)) as conn:
        def accept():
            pending = _winapi.ConnectNamedPipe(conn.fileno(), overlapped=True)
            try:
                pending.GetOverlappedResult(True)
            finally:
                pending.cancel()
            return conn
        yield accept


def _connect(directory):
    endpoint = address(directory)
    if os.name != "nt":
        return connection.Client(endpoint, family="AF_UNIX")
    import _winapi

    # Open a local instance once. SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION
    # prevents a server impersonating the executor. Busy/unavailable fails closed.
    handle = _winapi.CreateFile(endpoint, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0, 0,
                                _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED | 0x110000, 0)
    conn = connection.PipeConnection(handle)
    try:
        _winapi.SetNamedPipeHandleState(handle, _winapi.PIPE_READMODE_MESSAGE, None, None)
        return conn
    except OSError:
        conn.close()
        raise


def peer_pid(conn):
    """Read the client identity from the accepted local transport, never the hello."""
    if os.name == "nt":
        pid = ctypes.c_uint32()
        if not ctypes.windll.kernel32.GetNamedPipeClientProcessId(ctypes.c_void_p(conn.fileno()), ctypes.byref(pid)):
            raise OSError("cannot identify pipe client")
        return pid.value
    with socket.fromfd(conn.fileno(), socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        if sys.platform == "darwin":
            uid, gid = ctypes.c_uint(), ctypes.c_uint()
            if ctypes.CDLL(None).getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
                raise Rejected("cannot identify socket client")
            if uid.value != os.geteuid():
                raise Rejected("different OS user")
            # SOL_LOCAL, LOCAL_PEERPID
            return sock.getsockopt(0, 2)
        pid, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.geteuid():
            raise Rejected("different OS user")
        return pid


def _send(conn, obj):
    conn.send_bytes(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode())


def _receive(conn):
    if not conn.poll(TIMEOUT):
        raise TimeoutError("adapter response timed out")
    obj = json.loads(conn.recv_bytes(LIMIT).decode())
    if not isinstance(obj, dict):
        raise Rejected("invalid frame")
    return obj


def _proof(key, role, hello, nonce):
    # A fixed array of ASCII strings avoids object-key ordering across runtimes.
    fields = [role, hello["client"], nonce, *hello["vault"], hello["approval"], hello["op"]]
    return hmac.digest(key, json.dumps(fields, separators=(",", ":")).encode(), "sha256").hex()


def _request(op, approval, token):
    return {"version": 1, "session": token, "approval": approval, "op": op["id"], "path": op["path"],
            "base_sha256": op["base_sha256"], "append_b64": base64.b64encode(op["content"].encode()).decode(),
            "result_sha256": op["result_sha256"]}


def _vault_id(vault):
    return [str(vault.device), str(vault.inode)]


def exchange(directory, vault, approval="", op=None):
    """Probe, or submit exactly one approved operation. A lost reply is indeterminate."""
    hello = {"version": 1, "client": secrets.token_hex(32), "vault": _vault_id(vault),
             "approval": approval, "op": op["id"] if op else ""}
    with ExitStack() as stack:
        try:
            key = _boot_key(directory)
            conn = stack.enter_context(_connect(directory))
            _send(conn, hello)
            reply = _receive(conn)
            nonce = reply["nonce"]
            if (not re.fullmatch("[0-9a-f]{64}", nonce) or
                    not hmac.compare_digest(reply["proof"], _proof(key, "adapter", hello, nonce))):
                raise Rejected("invalid adapter proof")
        except (EOFError, KeyError, OSError, TypeError, ValueError) as exc:
            raise Disconnected("adapter unavailable") from exc
        if op is None:
            return "ok"
        token = _proof(key, "executor", hello, nonce)
        try:
            _send(conn, _request(op, approval, token))
            reply = _receive(conn)
            if reply.get("session") == token and reply.get("status") in ("ok", "conflict", "rejected"):
                return reply["status"]
        except (EOFError, OSError, ValueError):
            pass
        return "unknown"


def _approved_op(store, hello, vault, pid):
    """Resolve the operation only from the live approval and its file journal."""
    from core.approval import os_user, process_started_at

    claim = store.get_approval(hello["approval"])
    if (not claim or claim.kind != "brain.curate" or claim.status != "executing"
            or not claim.can_decide(os_user(), "cli") or claim.executor_pid != pid
            or claim.executor_started_at != process_started_at(pid) or not claim.payload
            or hashlib.sha256(claim.payload.encode()).hexdigest() != claim.payload_hash):
        raise Rejected("invalid executing claim")
    spec = json.loads(claim.payload)
    op = next((o for o in spec["ops"] if o["id"] == hello["op"]), None)
    files = {f.op_id: f for f in store.curate_file_results(claim.id)}
    file = files.get(hello["op"])
    if (spec["root_id"] != [vault.device, vault.inode] or not op or op["kind"] != "append"
            or not file or file.status is not None or file.base_sha256 != op["base_sha256"]
            or file.result_sha256 != op["result_sha256"]
            or any(files[d].status != "done" for d in op["depends_on"])):
        raise Rejected("unavailable file operation")
    curate._wiki_parts(op["path"])
    return spec, op


def serve_session(conn, key, vault, store, apply):
    """Serve one connection. apply(spec, approved_op) owns the conditional editor callback."""
    try:
        hello = _receive(conn)
        if (set(hello) != {"version", "client", "vault", "approval", "op"}
                or type(hello["version"]) is not int or hello["version"] != 1
                or hello["vault"] != _vault_id(vault)
                or not all(isinstance(hello[k], str) for k in ("client", "approval", "op"))
                or not re.fullmatch("[0-9a-f]{64}", hello["client"])):
            raise Rejected("invalid hello")
        pid = peer_pid(conn)
    except (OSError, ValueError):
        _send(conn, {"status": "rejected"})
        return
    nonce = secrets.token_hex(32)
    token = _proof(key, "executor", hello, nonce)
    _send(conn, {"nonce": nonce, "proof": _proof(key, "adapter", hello, nonce)})
    if not hello["approval"] and not hello["op"]:
        return
    try:
        request = _receive(conn)
    except EOFError:
        return  # Client closed before sending an operation.
    except ValueError:
        _send(conn, {"status": "rejected"})
        return
    session = request.get("session")
    if not isinstance(session, str) or not session.isascii() or not hmac.compare_digest(token, session):
        _send(conn, {"status": "rejected"})
        return
    try:
        spec, op = _approved_op(store, hello, vault, pid)
        if type(request.get("version")) is not int or request != _request(op, hello["approval"], token):
            raise Rejected("request differs from the approved operation")
    except (KeyError, OSError, ValueError):
        _send(conn, {"status": "rejected", "session": token})
        return
    # Failure after dispatch can be indeterminate. Let transport loss trigger reconciliation.
    try:
        status = apply(spec, op)
    except Conflict:
        status = "conflict"
    except Rejected:
        status = "rejected"
    if status not in ("ok", "conflict", "rejected"):
        raise ValueError("invalid adapter result")
    _send(conn, {"session": token, "status": status})
