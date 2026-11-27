import codecs
import hashlib
import os
import secrets
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from brain import append, curate
from brain.publish import pin_root
from core import approval
from core.note import approval_summary
from tests.test_curate_execution import decide, plan, propose
from tests.test_curate_proposal import EXAMPLE, EXISTING, append as append_row, create, write
from worklog import Store


def operation(base, addition, path=EXISTING):
    return {"kind": "append", "path": path, "content": addition.decode(),
            "base_sha256": hashlib.sha256(base).hexdigest(),
            "result_sha256": hashlib.sha256(base + addition).hexdigest()}


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
@pytest.mark.parametrize("terminated", [True, False])
def test_verified_append_preserves_exact_bytes(newline, terminated):
    base = codecs.BOM_UTF8 + EXAMPLE.replace(b"\n", newline)
    if not terminated:
        base = base.removesuffix(newline)
    addition = (b"" if terminated else newline) + "- 사실: 새 사실".encode() + newline
    op = operation(base, addition)
    assert append.verified_append(base, op, ["[[me/모형 전시]]"]) == base + addition
    with pytest.raises(append.Conflict):
        append.verified_append(base + b" ", op, [])
    with pytest.raises(ValueError):
        append.verified_append(base, op, ["[[me/새 근거]]"])
    with pytest.raises(ValueError):
        append.verified_append(base, dict(op, result_sha256="0" * 64), [])


@pytest.mark.parametrize("addition", [
    "- 사실: 하나\r\n", "- 사실: 하나", "---\n", "- 사실: 긴" + "a" * 2048 + "\n",
    "- 사실: 하나\n" * 4, "- 관계: [[대상]]\n" * 5,
])
def test_verified_append_rejects_invalid_lines_and_limits(addition):
    with pytest.raises(ValueError):
        append.verified_append(EXAMPLE, operation(EXAMPLE, addition.encode()), [])


def test_index_append_preserves_manual_text_and_checks_size():
    path = "javis/wiki/index.md"
    addition = b"\r\n- [[javis/wiki/topics/Topic]]\r\n"
    base = b"# Index\r\n" + b"x" * (32768 - len(addition) - len(b"# Index\r\n"))
    assert append.verified_append(base, operation(base, addition, path), []) == base + addition
    with pytest.raises(ValueError):
        append.verified_append(base + b"x", operation(base + b"x", addition, path), [])


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    write(root, EXISTING, EXAMPLE)
    # sockaddr_un has a small path limit, independent of pytest's basetemp.
    with tempfile.TemporaryDirectory(dir=None if os.name == "nt" else "/tmp") as private:
        directory = Path(private)
        monkeypatch.setenv("JAVIS_BRAIN_DIR", str(root))
        monkeypatch.setenv("JAVIS_DATA_DIR", private)
        monkeypatch.setattr(approval, "os_user", lambda: "owner")
        yield root, directory, Store.open(directory)


@pytest.fixture
def adapter(workspace, monkeypatch):
    root, directory, store = workspace
    key = secrets.token_bytes(32)
    try:
        (directory / "obsidian-append.key").write_bytes(append.registration(key))
    except FileNotFoundError as exc:
        # A Windows sandbox account can lack a DPAPI profile. Inject only the
        # boot key. The pipe ACL, peer PID and challenge/response remain real.
        if os.name != "nt" or exc.winerror != 2:
            raise
        monkeypatch.setattr(append, "_boot_key", lambda _: key)
    vault = pin_root(root)

    @contextmanager
    def listening(apply, *, server_key=key):
        with append.listen_once(directory) as accept, ThreadPoolExecutor(1) as pool:
            def serve():
                with accept() as conn:
                    append.serve_session(conn, server_key, vault, store, apply)

            server = pool.submit(serve)
            try:
                yield key
            finally:
                server.result(timeout=5)
    return listening


def test_preview_and_single_approved_append(workspace, adapter):
    root, _, store = workspace
    spec = plan(root, append_row(), sources=(EXISTING,))
    claim = propose(store, spec)
    writes = []

    def apply(approved, op):
        assert approved == spec
        target = root / op["path"]
        target.write_bytes(append.verified_append(target.read_bytes(), op, []))
        writes.append(op["id"])
        return "ok"

    with adapter(apply):
        assert "어댑터 연결됨" in approval_summary(store.get_approval(claim))
    assert writes == []
    with adapter(apply):
        assert decide(store, claim)[0] == "done"
    assert decide(store, claim)[0] == "already_decided"
    assert writes == ["P1"] and store.curate_result_counts(claim) == {"done": 1}
    assert (root / EXISTING).read_bytes() == EXAMPLE + spec["ops"][0]["content"].encode()


def test_wrong_adapter_key_receives_no_append(workspace, adapter, monkeypatch):
    root, _, store = workspace
    claim = propose(store, plan(root, append_row(), sources=(EXISTING,)))
    monkeypatch.setattr(append, "_request", lambda *_: pytest.fail("content sent before authentication"))
    with adapter(lambda *_: pytest.fail("untrusted adapter called"), server_key=secrets.token_bytes(32)):
        assert decide(store, claim)[0] == "failed"
    assert (root / EXISTING).read_bytes() == EXAMPLE


def test_executor_rechecks_inherited_provenance(workspace, monkeypatch):
    root, _, store = workspace
    source = curate.NOTES + "전시 준비.md"
    raw = EXAMPLE.replace("# 전시 점검".encode(), "# 전시 준비".encode())
    raw = raw.replace(b"[[me/", b"[[me/other-")
    write(root, source, raw)
    spec = plan(root, append_row(relations=[{"label": "", "target": "N2"}]), sources=(EXISTING, source))
    # This note is already a derived source. Its upstream original is new.
    spec["ops"][0]["sources"] = ["N2"]
    claim = propose(store, spec)
    monkeypatch.setattr(append, "exchange", lambda *_: pytest.fail("new provenance reached adapter"))
    assert decide(store, claim)[0] == "failed"
    assert (root / EXISTING).read_bytes() == EXAMPLE


@pytest.mark.parametrize("case", ["owner", "executor", "started", "session", "vault", "file",
                                  "bytes", "hash", "pending", "completed", "claim vault"])
def test_adapter_rejects_mismatched_request(workspace, adapter, monkeypatch, case):
    root, directory, store = workspace
    vault = pin_root(root)
    spec = plan(root, append_row(), sources=(EXISTING,))
    if case == "claim vault":
        spec["root_id"] = [0, 0]
    claim = propose(store, spec)
    executor = approval._executor_identity()
    if case == "executor":
        executor = (executor[0] + 1, executor[1])
    if case == "started":
        executor = (executor[0], "earlier")
    if case != "pending":
        store.decide_approval(claim, actor="owner", channel="cli", approve=True, executor=executor)
    if case == "completed":
        assert store.record_curate_result(claim, "P1", "done", executor=executor)
    if case == "owner":
        monkeypatch.setattr(approval, "os_user", lambda: "another user")
    hello = {"version": 1, "client": secrets.token_hex(32), "approval": claim, "op": "P1",
             "vault": ["0", "0"] if case == "vault" else [str(vault.device), str(vault.inode)]}
    with adapter(lambda *_: pytest.fail("unauthorized write")) as key, append._connect(directory) as conn:
        append._send(conn, hello)
        challenge = append._receive(conn)
        if case == "vault":
            assert challenge == {"status": "rejected"}
        else:
            token = append._proof(key, "executor", hello, challenge["nonce"])
            request = append._request(spec["ops"][0], claim, token)
            if case == "session":
                request["session"] = append._proof(key, "executor", hello, "0" * 64)
            if case == "file":
                request["path"] = curate.NOTES + "Other.md"
            if case == "bytes":
                request["append_b64"] = "YQ=="
            if case == "hash":
                request["result_sha256"] = "0" * 64
            append._send(conn, request)
            assert append._receive(conn)["status"] == "rejected"
    assert (root / EXISTING).read_bytes() == EXAMPLE


@pytest.mark.parametrize("behavior,expected", [("edited", "conflict"), ("false ok", "unknown"),
                                               ("lost reply", "done")])
def test_rechecks_bytes_at_callback_and_after_reply(workspace, adapter, behavior, expected):
    root, _, store = workspace
    spec = plan(root, append_row(), sources=(EXISTING,))
    claim = propose(store, spec)
    target = root / EXISTING
    edited = EXAMPLE + b"user edit"

    def apply(_, op):
        if behavior == "false ok":
            return "ok"
        if behavior == "edited":
            target.write_bytes(edited)
        target.write_bytes(append.verified_append(target.read_bytes(), op, []))
        raise ConnectionResetError("reply lost after write")

    if behavior == "lost reply":
        with pytest.raises(ConnectionResetError), adapter(apply):
            assert decide(store, claim)[0] == "done"
    else:
        with adapter(apply):
            assert decide(store, claim)[0] == "failed"
    assert store.curate_result_counts(claim) == {expected: 1}
    final = {"edited": edited, "false ok": EXAMPLE,
             "lost reply": EXAMPLE + spec["ops"][0]["content"].encode()}
    assert target.read_bytes() == final[behavior]


def test_disconnected_append_keeps_target_and_publishes_independent_create(workspace, monkeypatch):
    root, _, store = workspace
    spec = plan(root, append_row(), create("P2", "Independent"), sources=(EXISTING,))
    claim = propose(store, spec)
    target = root / EXISTING
    before = target.stat()
    assert "어댑터 미연결" in approval_summary(store.get_approval(claim))
    published = []
    publish = curate._publish_new

    def observe(vault, path, *args):
        published.append(path)
        assert path != EXISTING
        return publish(vault, path, *args)

    monkeypatch.setattr(curate, "_publish_new", observe)
    status, message = decide(store, claim)
    assert status == "failed" and "추가 기능 미연결" in message
    assert store.curate_result_counts(claim) == {"done": 1, "failed": 1}
    assert target.read_bytes() == EXAMPLE
    assert os.path.samestat(before, target.stat()) and before.st_mtime_ns == target.stat().st_mtime_ns
    new = spec["ops"][1]
    assert published == [new["path"]] and (root / new["path"]).read_bytes() == new["content"].encode()
