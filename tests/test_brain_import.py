import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from brain import importer
from brain.policy import Rejected
from core import cli
from core.engine import ClaudeEngine
from tests.test_core_engine import fake_claude, success
from tests.test_telegram import api, bot, owner_update
from worklog import Store


@pytest.fixture
def roots(tmp_path, monkeypatch):
    src = tmp_path.resolve() / "source"
    vault = src.with_name("vault")
    src.mkdir()
    vault.mkdir()
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(vault))
    monkeypatch.setenv("JAVIS_DATA_DIR", str(src.with_name("data")))
    return src, vault


def _write(root, relative, content=b"test"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _snapshot(root):
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes() if path.is_file() else None, path.stat().st_mtime_ns,
        )
        for path in (root, *root.rglob("*"))
    }


def test_manifest_preserves_paths_and_trees(roots):
    src, vault = roots
    body = b"example\r\n\xff"
    _write(src, "a/b/c.md", body)
    _write(src, "Empty.MD", b"")
    _write(vault, "unrelated.md")
    before = [_snapshot(root) for root in roots]

    manifest = importer.build_manifest(src, r"imported\batch")

    assert manifest["dest"] == "imported/batch"
    assert manifest["files"][1] == {
        "src": "a/b/c.md",
        "dest": "imported/batch/a/b/c.md",
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "status": "copy",
    }
    assert manifest["totals"] == {"count": 2, "bytes": len(body)}
    assert manifest["excluded"] == {}
    for key, root in zip(("source_root", "brain_root"), roots):
        info = root.stat()
        assert manifest[key] == {"realpath": str(root), "st_dev": info.st_dev, "st_ino": info.st_ino}
    assert json.loads(json.dumps(manifest)) == manifest
    assert [_snapshot(root) for root in roots] == before


def test_explicit_relative_source_is_resolved(roots, monkeypatch):
    src, vault = roots
    _write(src, "note.md")
    monkeypatch.chdir(vault)
    name = src.name.upper() if os.name == "nt" else src.name
    manifest = importer.build_manifest(f"../{name}", "imports")
    assert manifest["source_root"]["realpath"] == str(src)
    assert manifest["files"][0]["src"] == "note.md"


def test_excluded_entries_are_counted_without_descending(roots, monkeypatch):
    src, _ = roots
    for name in (".obsidian/config.md", ".hidden.md", "BRAIN.md", "agents.MD", "photo.png", "junction/note.md"):
        _write(src, name)
    original = os.lstat

    def lstat(path, **kwargs):
        info = original(path, **kwargs)
        if Path(path).name == "junction":
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        if Path(path).name in ("config.md", "note.md"):
            pytest.fail("opened an excluded directory")
        return info

    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", lstat)
        manifest = importer.build_manifest(src, "imports")
    assert manifest["files"] == []
    assert manifest["excluded"] == {"hidden": 2, "control": 2, "attachment": 1, "link": 1}


def test_symlinks_are_excluded_and_link_ancestors_rejected(roots):
    src, _ = roots
    _write(src.parent, "outside/nested/other.md")
    outside = src.parent / "outside"
    try:
        (src / "linked.md").symlink_to(outside / "nested/other.md")
        (src / "linked").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        if os.name == "nt" and error.winerror in (5, 1314):
            pytest.skip("Windows symlink permission unavailable")
        raise
    assert importer.build_manifest(src, "imports")["excluded"] == {"link": 2}
    for source in (src / "linked/nested", src / "linked/../outside"):
        with pytest.raises(Rejected):
            importer.build_manifest(source, "imports")


def test_reparse_ancestor_rejected(roots, monkeypatch):
    src, _ = roots
    (src / "junction/nested").mkdir(parents=True)
    original = os.lstat

    def lstat(path, **kwargs):
        info = original(path, **kwargs)
        if Path(path).name == "junction":
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        if Path(path).name == "nested":
            pytest.fail("inspected a path below a reparse point")
        return info

    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", lstat)
        with pytest.raises(Rejected):
            importer.build_manifest(src / "junction/nested", "imports")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX special files")
def test_special_file_is_excluded(roots):
    src, _ = roots
    os.mkfifo(src / "pipe.md")
    assert importer.build_manifest(src, "imports")["excluded"] == {"special": 1}


def test_existing_files_are_conflicts_including_empty_and_case_variants(roots):
    src, vault = roots
    for name in ("regular.md", "empty.md", "CASE.md", "new.md"):
        _write(src, f"nested/{name}")
    _write(vault, "imports/nested/regular.md")
    _write(vault, "imports/nested/empty.md", b"")
    _write(vault, "imports/nested/case.MD")

    manifest = importer.build_manifest(src, "imports")

    assert {row["src"]: row["status"] for row in manifest["files"]} == {
        "nested/CASE.md": "conflict", "nested/empty.md": "conflict",
        "nested/new.md": "copy", "nested/regular.md": "conflict",
    }
    assert manifest["totals"] == {"count": 4, "bytes": 16}


def test_case_variants_in_same_import_conflict(roots):
    src, _ = roots
    _write(src, "nested/Note.md")
    if (src / "nested/note.md").exists():
        pytest.skip("source filesystem is case-insensitive")
    _write(src, "nested/note.md")

    manifest = importer.build_manifest(src, "imports")

    assert [row["dest"] for row in manifest["files"]] == [
        "imports/nested/Note.md", "imports/nested/note.md",
    ]
    assert sorted(row["status"] for row in manifest["files"]) == ["conflict", "copy"]


def test_overlapping_roots_rejected(roots, monkeypatch):
    src, vault = roots
    inside_vault = vault / "source"
    inside_vault.mkdir()
    data = vault.with_name("data")
    data.mkdir()
    for source in (vault, inside_vault, src.parent, data, importer._checkout()):
        with pytest.raises(Rejected, match="겹칠 수 없습니다"):
            importer.build_manifest(source, "imports")
    monkeypatch.setattr(importer, "_checkout", lambda: src / "repo")
    with pytest.raises(Rejected, match="겹칠 수 없습니다"):
        importer.build_manifest(src, "imports")


@pytest.mark.parametrize("dest", [
    "/imports", r"C:\imports", "imports/../other", "CON/imports", "imports:stream",
    "IMPORT~1", "imports.", "imports ",
])
def test_invalid_destinations_rejected(roots, dest):
    src, _ = roots
    with pytest.raises(Rejected):
        importer.build_manifest(src, dest)


@pytest.mark.parametrize("policy", [b"imports\n../other", b"\xff", b"", b"other"])
def test_invalid_or_disallowing_policy_rejected_even_with_empty_source(roots, policy):
    src, vault = roots
    _write(vault, "BRAIN.md", policy)
    with pytest.raises(Rejected, match="BRAIN.md"):
        importer.build_manifest(src, "imports")


def test_policy_applies_to_each_target(roots):
    src, vault = roots
    _write(vault, "BRAIN.md", b"imports/allowed\r\n")
    _write(src, "allowed/note.md")
    assert importer.build_manifest(src, "imports")["totals"]["count"] == 1
    _write(src, "other/note.md")
    with pytest.raises(Rejected, match="BRAIN.md"):
        importer.build_manifest(src, "imports")


@pytest.mark.parametrize(("limit", "maximum"), [
    ("MAX_FILES", 1), ("MAX_FILE_BYTES", 3), ("MAX_TOTAL_BYTES", 7),
])
def test_limit_overrun_rejects_whole_manifest(roots, monkeypatch, limit, maximum):
    src, _ = roots
    _write(src, "a.md")
    _write(src, "b.md")
    monkeypatch.setattr(importer, limit, maximum)
    with pytest.raises(Rejected, match="범위를 줄이세요"):
        importer.build_manifest(src, "imports")


def _propose(src, capsys):
    store = Store.open(src.with_name("data"))
    assert cli.main(["brain", "import", str(src), "--dest", "imports/batch"]) == 0
    [draft] = store.list_approvals("pending")
    assert draft.id in capsys.readouterr().out
    return store, draft


def _assert_finished(store, draft, status):
    row = store.get_approval(draft.id)
    assert (row.status, row.payload, row.payload_hash) == (status, None, None)


def test_import_reject_approve_and_repeat(roots, capsys):
    src, vault = roots
    _write(src, "nested/a.md", b"original\r\n\xff")
    _write(src, "empty.md", b"")
    original = _snapshot(src)
    store, draft = _propose(src, capsys)
    assert (draft.kind, draft.owner, draft.allowed_channel) == ("brain.import", cli._os_user(), "cli")
    assert json.loads(draft.payload) == importer.build_manifest(src, "imports/batch")
    assert cli.main(["reject", draft.id]) == 0
    assert list(vault.iterdir()) == []
    _assert_finished(store, draft, "rejected")

    store, draft = _propose(src, capsys)
    assert cli.main(["approve", draft.id]) == 0
    assert "imports/batch · published 2 · conflict 0 · changed 0 · failed 0" in capsys.readouterr().out
    assert {p.relative_to(vault).as_posix(): p.read_bytes() for p in vault.rglob("*.md")} == {
        "imports/batch/nested/a.md": b"original\r\n\xff",
        "imports/batch/empty.md": b"",
    }
    saved = _snapshot(vault)
    assert cli.main(["approve", draft.id]) == 1
    assert _snapshot(vault) == saved
    assert _snapshot(src) == original
    _assert_finished(store, draft, "done")

    assert cli.main(["brain", "import", str(src), "--dest", "imports/batch"]) == 0
    assert "승인을 제안하지 않았습니다" in capsys.readouterr().out
    assert store.list_approvals("pending") == []


def test_import_uses_only_unchanged_approved_files(roots, capsys):
    src, vault = roots
    for name in ("changed.md", "missing.md", "collision.md", "ok.md", "skipped.md"):
        _write(src, name)
    _write(vault, "imports/batch/skipped.md", b"existing")
    store, draft = _propose(src, capsys)
    _write(src, "changed.md", b"edit")  # Same size, different hash.
    (src / "missing.md").unlink()
    _write(src, "new.md")
    _write(vault, "imports/batch/collision.md", b"new existing file")
    (vault / "imports/batch/skipped.md").unlink()
    original = _snapshot(src)

    assert cli.main(["approve", draft.id]) == 0
    assert "published 1 · conflict 2 · changed 2 · failed 0" in capsys.readouterr().out
    target = vault / "imports/batch"
    assert {p.name: p.read_bytes() for p in target.iterdir()} == {
        "collision.md": b"new existing file", "ok.md": b"test",
    }
    assert _snapshot(src) == original
    _assert_finished(store, draft, "done")


def test_import_partial_failure_and_reproposal(roots, capsys, monkeypatch):
    src, vault = roots
    for name in ("a.md", "b.md", "c.md"):
        _write(src, name, name.encode())
    original = _snapshot(src)
    store, draft = _propose(src, capsys)
    publish = importer.publisher.publish

    def fail_middle(root, path, content, **kwargs):
        if path.endswith("/b.md"):
            raise RuntimeError("simulated publication failure")
        return publish(root, path, content, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(importer.publisher, "publish", fail_middle)
        assert cli.main(["approve", draft.id]) == 0
    assert "published 2 · conflict 0 · changed 0 · failed 1" in capsys.readouterr().out
    _assert_finished(store, draft, "done")
    target = vault / "imports/batch"
    before_retry = _snapshot(target)
    assert {p.name for p in target.iterdir()} == {"a.md", "c.md"}

    store, retry = _propose(src, capsys)
    assert cli.main(["approve", retry.id]) == 0
    assert "published 1 · conflict 2 · changed 0 · failed 0" in capsys.readouterr().out
    assert all(_snapshot(target)[name] == before_retry[name] for name in ("a.md", "c.md"))
    assert (target / "b.md").read_bytes() == b"b.md"
    assert _snapshot(src) == original
    _assert_finished(store, retry, "done")


@pytest.mark.parametrize("invalidated", ["source", "vault", "invalid policy", "restrictive policy"])
def test_import_fails_when_root_or_policy_changes(roots, capsys, invalidated):
    src, vault = roots
    _write(src, "a.md")
    store, draft = _propose(src, capsys)
    if invalidated in ("source", "vault"):
        root = src if invalidated == "source" else vault
        root.rename(root.with_name("original"))
        root.mkdir()
        if root == src:
            _write(src, "a.md")
    else:
        _write(vault, "BRAIN.md", b"../bad" if invalidated == "invalid policy" else b"only-here")

    assert cli.main(["approve", draft.id]) == 1
    assert "published 0 · conflict 0 · changed 0 · failed 1" in capsys.readouterr().err
    assert not (vault / "imports").exists()
    _assert_finished(store, draft, "failed")


def test_import_payload_stays_local(roots, api, capsys):
    src, vault = roots
    name, body = "ImportFileSentinel.md", "ImportBodySentinel"
    _write(src, name, body.encode())
    store, draft = _propose(src, capsys)
    capture = src.with_name("engine-stdin.txt")
    envelope = success([{"text": "control answer", "sources": []}])
    shim = fake_claude(src.parent, f"""
Path({str(capture)!r}).write_text(question, encoding="utf-8")
print(json.dumps({envelope!r}))
""")
    engine = ClaudeEngine(cli.load_settings(), shim)
    api.updates(
        owner_update(40, "승인 대기"),
        owner_update(41, f"승인 {draft.id}"),
        owner_update(42, f"거절 {draft.id}"),
    )
    for _ in range(3):
        api.reply()
    bot(api, store, engine).poll_once()
    pending, approval, rejection = [request["text"] for request in api.sent()]
    assert draft.id not in pending and "승인 대기 0건" in pending
    assert "PC에서만" in approval and "PC에서만" in rejection
    assert store.get_approval(draft.id) == draft
    assert not list(vault.iterdir())
    assert cli.main(["approve", draft.id]) == 0
    assert not capture.exists()
    _assert_finished(store, draft, "done")
    with store._connect() as db:
        surfaces = repr(api.sent()) + repr(capsys.readouterr())
        for query in ("SELECT * FROM records", "SELECT * FROM deliveries"):
            surfaces += repr([tuple(row) for row in db.execute(query)])
    assert name not in surfaces and body not in surfaces
