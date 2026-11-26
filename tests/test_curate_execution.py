import hashlib
import json
import os
import subprocess
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

import pytest

import brain.curate as curate
import brain.publish as publishing
from core import approval
from core.approval import ApprovalService
from core.curate import build_proposal
from core.note import NOTE_EXECUTORS, approval_summary, decide_on_pc
from tests.test_curate_proposal import EXAMPLE, EXISTING, append, create, prepare, write
from worklog import Store


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "vault"
    root.mkdir()
    data = tmp_path / "data"
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(root))
    monkeypatch.setenv("JAVIS_DATA_DIR", str(data))
    monkeypatch.setattr(approval, "os_user", lambda: "owner")
    return root, Store.open(data)


def plan(root, *rows, sources=()):
    return json.loads(build_proposal(prepare(root, *sources), {"ops": list(rows)}).payload)


def index_op(key, path, *deps):
    content = "".join(f"- {curate.note_link(op['path'])}\n" for op in deps)
    return {"id": key, "depends_on": [op["id"] for op in deps], "kind": "create",
            "path": path, "absent": True, "content": content,
            "result_sha256": hashlib.sha256(content.encode()).hexdigest()}


def add_indexes(spec):
    ops = spec["ops"]
    topic = index_op("P3", curate.TOPICS + "Topic.md", *ops)
    ops.extend([topic, index_op("P4", "javis/wiki/index.md", topic)])


def propose(store, spec):
    return ApprovalService(store).propose("brain.curate", json.dumps(spec, ensure_ascii=False), "owner", "cli").id


def decide(store, key):
    return decide_on_pc(store, ApprovalService(store, NOTE_EXECUTORS), key, "owner", approve=True)


def test_boundary_name_and_dependency_order(workspace):
    root, store = workspace
    base = "가" * 37 + " abc"
    write(root, "me/" + base + ".md", b"original")
    spec = plan(root, create(title=base))
    add_indexes(spec)
    name = base + "-2.md"
    assert len(name.encode()) == 120 and spec["ops"][0]["path"] == curate.NOTES + name
    key = propose(store, spec)
    assert not (root / "javis").exists()
    assert decide(store, key)[0] == "done"
    assert store.curate_result_counts(key) == {"done": 3}
    for op in spec["ops"]:
        assert (root / op["path"]).read_bytes() == op["content"].encode()
    assert [p.name for p in (root / curate.NOTES).iterdir()] == [name]
    assert decide(store, key)[0] == "already_decided"


@pytest.mark.parametrize("path", [
    curate.NOTES + "가" * 37 + " abcd-2.md", curate.NOTES + "x?.md", curate.NOTES + "CoM³.md",
    curate.NOTES + "cOn.md", curate.NOTES + "../outside.md", "javis/inbox/other.md",
    "javis/wiki/other.md", curate.NOTES + "nested/path.md",
])
def test_invalid_later_path_prevents_all_writes(workspace, path, monkeypatch):
    root, store = workspace
    spec = plan(root, create(), create("P2", "Other"))
    spec["ops"][1]["path"] = path
    monkeypatch.setattr(curate, "_publish_new", lambda *args: pytest.fail("invalid batch reached publisher"))
    key = propose(store, spec)
    assert decide(store, key)[0] == "failed"
    assert store.curate_result_counts(key) == {"failed": 2}
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("directory", [curate.NOTES, curate.TOPICS, "me/"])
def test_approved_suffix_taken_before_publication(workspace, directory, monkeypatch):
    root, store = workspace
    write(root, "me/검사 A.md", b"original")
    spec = plan(root, create(title="검사 A"), create("P2", "Independent"))
    add_indexes(spec)
    key = propose(store, spec)
    alias = directory + unicodedata.normalize("NFD", "검사 a-2.MD")
    completed_temp = publishing._completed_temp

    @contextmanager
    def concurrent_edit(*args):
        with completed_temp(*args) as temp:
            write(root, alias, b"existing")
            yield temp

    monkeypatch.setattr(publishing, "_completed_temp", concurrent_edit)
    status, message = decide(store, key)
    assert status == "failed" and "선행 미완료 2" in message
    assert store.curate_result_counts(key) == {"conflict": 1, "done": 1, "dependency_incomplete": 2}
    assert (root / alias).read_bytes() == b"existing"
    assert (root / "me/검사 A.md").read_bytes() == b"original"
    assert {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} == {
        alias, "me/검사 A.md", spec["ops"][1]["path"],
    }


def test_changed_source_and_unconnected_append_preserve_other_files(workspace):
    root, store = workspace
    write(root, EXISTING, EXAMPLE)
    write(root, "me/Input.md", b"source")
    spec = plan(root, create(sources=["N2"]), append("P2"), create("P3", "Independent"),
                sources=(EXISTING, "me/Input.md"))
    key = propose(store, spec)
    write(root, "me/Input.md", b"edit")
    status, message = decide(store, key)
    assert status == "failed" and "실패: 추가 기능 미연결" in message
    assert store.curate_result_counts(key) == {"failed": 2, "done": 1}
    assert (root / EXISTING).read_bytes() == EXAMPLE
    assert not (root / spec["ops"][0]["path"]).exists()
    assert (root / spec["ops"][2]["path"]).exists()


def test_two_approvals_share_vault_name_lock(workspace, monkeypatch):
    root, store = workspace
    note = plan(root, create(title="Shared"))
    topic = plan(root, create(title="shared"))
    topic["ops"][0]["path"] = curate.TOPICS + "shared.md"
    keys = [propose(store, spec) for spec in (note, topic)]
    barrier = Barrier(2)
    publish = curate._publish_new

    def together(*args):
        barrier.wait(timeout=10)
        return publish(*args)

    monkeypatch.setattr(curate, "_publish_new", together)
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(lambda key: decide(store, key)[0], keys))
    assert sorted(outcomes) == ["done", "failed"]
    assert store.curate_result_counts(keys[outcomes.index("failed")]) == {"conflict": 1}
    assert len(list(root.rglob("*.md"))) == 1


@pytest.mark.parametrize("changed", [False, True])
def test_recover_process_exit_after_publish_without_replaying(workspace, changed):
    root, store = workspace
    spec = plan(root, create())
    add_indexes(spec)
    key = propose(store, spec)
    script = """
import os, sys
from pathlib import Path
from worklog import Store
from core.approval import ApprovalService
from core.note import decide_on_pc
store = Store.open(Path(os.environ["JAVIS_DATA_DIR"]))
service = ApprovalService(store)
service.record_curate_result = lambda *args: os._exit(73)
decide_on_pc(store, service, sys.argv[1], "owner", approve=True)
"""
    assert subprocess.run([sys.executable, "-c", script, key], timeout=30).returncode == 73
    target = root / spec["ops"][0]["path"]
    approved = spec["ops"][0]["content"].encode()
    assert target.read_bytes() == approved
    if changed:
        target.write_bytes(b"edited")
    assert store.get_approval(key).status == "executing"
    preview = approval_summary(store.get_approval(key))
    assert "미적용 2" in preview and ("결과 미상 1" if changed else "완료 1") in preview
    status, message = decide(store, key)
    assert status == "failed" and "미적용 2" in message
    assert ("결과 미상 1" if changed else "완료 1") in message
    assert store.get_approval(key).payload is None
    assert store.curate_result_counts(key) == {"unknown" if changed else "done": 1, "failed": 2}
    assert decide(store, key)[0] == "already_decided"
    assert list(root.rglob("*.md")) == [target]
    assert target.read_bytes() == (b"edited" if changed else approved)


def test_interrupted_preview_preserves_recorded_outcomes(workspace, monkeypatch):
    root, store = workspace
    spec = plan(root, create(), create("P2", "Other"))
    add_indexes(spec)
    done, conflict, topic, index = spec["ops"]
    key = propose(store, spec)
    executor = (os.getpid(), "previous-start")
    claim = store.decide_approval(key, actor="owner", channel="cli", approve=True, executor=executor).approval
    for op, status in [(done, "done"), (conflict, "conflict"), (topic, "dependency_incomplete")]:
        assert store.record_curate_result(key, op["id"], status, executor=executor)
    write(root, done["path"], b"edited after publication")
    write(root, conflict["path"], b"existing note")

    reconciled = []

    def reconcile(vault, op, base, result):
        reconciled.append(op["id"])
        return curate.reconcile_file(vault, op, base, result)

    monkeypatch.setattr("core.note.reconcile_file", reconcile)
    before = store.curate_file_results(key)
    preview = approval_summary(claim)
    assert reconciled == [index["id"]]
    assert store.curate_file_results(key) == before
    for op, label in [(done, "완료"), (conflict, "충돌"), (topic, "선행 미완료"), (index, "미적용")]:
        assert f"{op['path']} · {label}" in preview.splitlines()
    status, report = decide(store, key)
    assert status == "failed" and report == preview.rsplit("\n", 1)[0]


def test_reject_interrupted_curate_leaves_recovery_for_approval(workspace):
    root, store = workspace
    spec = plan(root, create())
    key = propose(store, spec)
    claim = store.decide_approval(key, actor="owner", channel="cli", approve=True,
                                  executor=(os.getpid(), "previous-start")).approval
    op, = spec["ops"]
    write(root, op["path"], op["content"].encode())
    before = store.curate_file_results(key)

    status, message = decide_on_pc(store, ApprovalService(store), key, "owner", approve=False)
    assert status == "already_decided" and "같은 ID로 승인하면 대조 결과를 마감" in (message or "")
    assert store.get_approval(key) == claim
    assert store.curate_file_results(key) == before
    assert decide(store, key)[0] == "done"
    assert store.curate_result_counts(key) == {"done": 1}


def test_redirected_parent_does_not_write_outside(workspace):
    root, store = workspace
    outside = root.parent / "outside"
    outside.mkdir()
    spec = plan(root, create())
    key = propose(store, spec)
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(root / "javis"))
    else:
        (root / "javis").symlink_to(outside, target_is_directory=True)
    assert decide(store, key)[0] == "failed"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("change", ["policy", "parent", "source"])
def test_rechecks_approval_before_exclusive_publish(workspace, monkeypatch, change):
    root, store = workspace
    write(root, "me/Input.md", b"original")
    write(root, "BRAIN.md", b"javis/wiki\n")
    spec = plan(root, create(sources=["N1"]), sources=("me/Input.md",))
    key = propose(store, spec)
    directory = root / curate.NOTES
    directory.mkdir(parents=True)
    if change == "parent":
        publish = curate._publish_new

        def replaced(*args):
            directory.rename(directory.with_name("old"))
            directory.mkdir()
            return publish(*args)

        monkeypatch.setattr(curate, "_publish_new", replaced)
    else:
        completed_temp = publishing._completed_temp

        @contextmanager
        def edited(*args):
            with completed_temp(*args) as temp:
                if change == "policy":
                    write(root, "BRAIN.md", b"javis/wiki\r\n")
                else:
                    write(root, "me/Input.md", b"edited")
                yield temp

        monkeypatch.setattr(publishing, "_completed_temp", edited)
    assert decide(store, key)[0] == "failed"
    assert list(directory.iterdir()) == []
    assert not list((root / "javis").rglob("*.md"))


@pytest.mark.parametrize("change", ["missing dependency", "phase order", "result hash", "oversize index"])
def test_preflights_later_index_before_publishing_notes(workspace, change):
    root, store = workspace
    spec = plan(root, create())
    add_indexes(spec)
    note, topic, index = spec["ops"]
    if change == "missing dependency":
        topic["depends_on"] = []
    elif change == "phase order":
        topic["content"] = "# Topic\n"
        topic["result_sha256"] = hashlib.sha256(topic["content"].encode()).hexdigest()
        topic["depends_on"] = []
        spec["ops"] = [topic, note, index]
    elif change == "result hash":
        index["result_sha256"] = "0" * 64
    else:
        index["content"] = "x" * 32769
        index["result_sha256"] = hashlib.sha256(index["content"].encode()).hexdigest()
    key = propose(store, spec)
    assert decide(store, key)[0] == "failed"
    assert list(root.iterdir()) == []
