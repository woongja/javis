"""Local desktop plugin helper. Only the editor can publish the prospective text."""

import hashlib
import json
import os
import queue
import secrets
import stat
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from brain import append, curate
from brain.policy import Rejected
from brain.publish import VaultRoot, _checked_root, _policy_bytes, _target_directory, pin_configured_root
from core.approval import process_started_at
from core.config import data_dir
from worklog import Store


def checked_base(vault, guard):
    """Called synchronously by the editor, with the original, lossless Python pins."""
    op = guard["op"]
    with ExitStack() as stack:
        root = _checked_root(stack, vault)
        raw = _policy_bytes(root)
        if ("absent" if raw is None else hashlib.sha256(raw).hexdigest()) != guard["policy"]:
            raise Rejected("policy changed")
        parents = curate._parent_ids(root, curate._wiki_parts(op["path"]))
        if list(map(list, parents)) != guard["parents"]:
            raise Rejected("parent changed")
        file = curate._target_snapshot(vault, op["path"])
        if not file or list(file.identity) != op["file_id"]:
            raise Rejected("file changed")
        file.content.decode("utf-8")  # Invalid disk bytes are never an editor conflict.
        append.verified_append(file.content, op, [])
        if time.monotonic() >= guard["deadline"] or process_started_at(guard["pid"]) != guard["started"]:
            raise Rejected("editor request expired")
        return file.content


def edit(vault, spec, op, replies, conn):
    ticket = secrets.token_hex(16)
    deadline = time.monotonic() + append.TIMEOUT
    with ExitStack() as stack:
        parts = curate._wiki_parts(op["path"])
        try:
            pid = append.peer_pid(conn)
            root = _checked_root(stack, vault, hold=True)
            _target_directory(stack, root, parts[:-1], create=False, hold=True)
            parents = list(map(list, curate._parent_ids(root, parts)))
            guard = {"root": spec["root_id"], "parents": parents, "policy": spec["policy_hash"],
                     "op": op, "deadline": deadline, "pid": pid, "started": process_started_at(pid)}
            checked_base(vault, guard)  # Check before allowing the editor to open the file.
        except append.Conflict:
            raise
        except (OSError, ValueError) as exc:
            raise Rejected("unsafe target") from exc
        # JS returns the original serialized guard. Inode values can exceed Number.MAX_SAFE_INTEGER.
        message = {"ticket": ticket, "guard": json.dumps(guard)}
        print(json.dumps(message), flush=True)
        while True:
            try:
                reply = replies.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError("editor reply lost") from None
            if reply["ticket"] == ticket:
                return reply["status"]  # An indeterminate result closes the client connection.


def read_replies(replies):
    try:
        for line in sys.stdin.buffer:
            replies.put(json.loads(line))
    finally:
        # Obsidian exited, possibly while accept() was blocked. Kernel locks are released.
        os._exit(0)


@contextmanager
def registration(directory):
    """Hold sole ownership across one-shot listeners, including crash recovery."""
    endpoint = append.address(directory)
    lock = directory / "obsidian-append.lock"
    with lock.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            socket = Path(endpoint)
            if socket.exists():
                info = socket.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                    raise Rejected("unsafe socket")
                socket.unlink()
        key = secrets.token_bytes(32)
        path = directory / "obsidian-append.key"
        tmp = tempfile.NamedTemporaryFile(dir=directory, delete=False)
        try:
            with tmp:
                tmp.write(append.registration(key))
            os.replace(tmp.name, path)
        finally:
            Path(tmp.name).unlink(missing_ok=True)
        try:
            yield key
        finally:
            path.unlink(missing_ok=True)


def serve(path):
    vault = pin_configured_root()
    if path != vault.path:
        raise Rejected("different editor vault")
    directory = data_dir()
    directory.mkdir(mode=0o700, exist_ok=True, parents=True)
    store = Store(directory / "javis.db")
    replies = queue.Queue()
    threading.Thread(target=read_replies, args=(replies,), daemon=True).start()
    with registration(directory) as key, ExitStack() as listeners:
        # Keep the Unix accept queue bound between consecutive file operations.
        unix_accept = listeners.enter_context(append.listen_once(directory)) if os.name != "nt" else None
        while True:
            with ExitStack() as session:
                accept = unix_accept or session.enter_context(append.listen_once(directory))
                conn = session.enter_context(accept())
                try:
                    append.serve_session(conn, key, vault, store,
                                         lambda spec, op: edit(vault, spec, op, replies, conn))
                except (EOFError, KeyError, OSError, TypeError, ValueError):
                    # Malformed/disconnected session or uncertain publication. Never retry.
                    pass


def main():
    mode, root = sys.argv[1:]
    if mode == "serve":
        serve(Path(root))
    elif mode == "check":
        guard = json.load(sys.stdin.buffer)
        vault = VaultRoot(Path(root), *guard["root"])
        try:
            sys.stdout.buffer.write(checked_base(vault, guard))
        except append.Conflict:
            sys.exit(2)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
