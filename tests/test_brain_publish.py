import errno
import os
import stat
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

import brain.publish as publishing
from brain.publish import pin_root, publish
from core import cli
from worklog import Store

INBOX = "javis/inbox"
NOTE = f"{INBOX}/Note.md"


@pytest.fixture
def vault(tmp_path):
    root = tmp_path.resolve() / "vault"
    (root / INBOX).mkdir(parents=True)
    return pin_root(root)


@pytest.mark.parametrize(("existing", "content"), [
    ("Note.md", b"original\r\n"),
    ("Note.md", b""),
    ("note.MD", b"case collision"),
])
def test_existing_file_is_preserved(vault, existing, content):
    directory = vault.path / INBOX
    (directory / existing).write_bytes(content)

    assert publish(vault, NOTE, b"replacement").status == "conflict"
    assert [path.name for path in directory.iterdir()] == [existing]
    assert (directory / existing).read_bytes() == content


@pytest.mark.parametrize("other", [NOTE, f"{INBOX}/note.MD"])
def test_concurrent_publish_has_one_winner(vault, other):
    ready = Barrier(2)

    def send(path, content):
        ready.wait(timeout=10)
        return publish(vault, path, content)

    payloads = [b"first\n" * 1000, b"second\r\n" * 1000]
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(send, path, content) for path, content in zip((NOTE, other), payloads)]
        statuses = [job.result().status for job in jobs]
    assert sorted(statuses) == ["conflict", "published"]
    [target] = (vault.path / INBOX).iterdir()
    assert target.read_bytes() == payloads[statuses.index("published")]


@pytest.mark.parametrize("relative_path", [
    "../note.md", f"{INBOX}/../note.md", "/note.md", r"C:\note.md", r"\note.md",
    r"\\?\C:\note.md", f"{INBOX}/CON.md", f"{INBOX}/NUL/note.md",
    f"{INBOX}/LPT².md", f"{INBOX}/note.md:stream", f"{INBOX}/note.md ",
    f"{INBOX}/alias./note.md", f"{INBOX}/LONGNA~1.md", f"{INBOX}/BRAIN.md",
    f"{INBOX}/agents.md", f"{INBOX}/.hidden/note.md", f"{INBOX}/.hidden.md",
    f"{INBOX}/attachment.png", f"{INBOX}/bad\x00name.md",
])
def test_unsafe_paths_are_rejected(vault, relative_path):
    assert publish(vault, relative_path, b"note").status == "rejected"
    assert list((vault.path / INBOX).iterdir()) == []


def test_native_absolute_path_is_rejected(vault):
    assert publish(vault, str(vault.path / NOTE), b"note").status == "rejected"
    assert not (vault.path / NOTE).exists()


def test_symlink_ancestor_and_target_are_rejected(vault):
    outside = vault.path.parent / "outside"
    outside.mkdir()
    target = outside / "note.md"
    target.write_bytes(b"original")
    try:
        (vault.path / INBOX / "linked").symlink_to(outside, target_is_directory=True)
        (vault.path / NOTE).symlink_to(target)
    except OSError as error:
        if os.name == "nt" and error.winerror in (5, 1314):
            pytest.skip("Windows symlink permission unavailable")
        raise

    assert publish(vault, f"{INBOX}/linked/new.md", b"note").status == "rejected"
    assert publish(vault, NOTE, b"note").status == "rejected"
    assert list(outside.iterdir()) == [target]
    assert target.read_bytes() == b"original"


@pytest.mark.parametrize("attribute", [stat.FILE_ATTRIBUTE_REPARSE_POINT, stat.FILE_ATTRIBUTE_HIDDEN])
def test_reparse_or_hidden_ancestor_is_rejected(vault, monkeypatch, attribute):
    original = os.lstat

    def lstat(path, **kwargs):
        info = original(path, **kwargs)
        if Path(path).name == "inbox":
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=attribute)
        return info

    monkeypatch.setattr(os, "lstat", lstat)
    assert publish(vault, NOTE, b"note").status == "rejected"
    assert list((vault.path / INBOX).iterdir()) == []


def test_default_policy_allows_inbox_and_the_approved_import_directory(vault):
    assert publish(vault, NOTE, b"note\r\n").status == "published"
    assert (vault.path / NOTE).read_bytes() == b"note\r\n"
    assert publish(vault, "imports/a.md", b"import").status == "rejected"
    assert not (vault.path / "imports").exists()
    assert publish(vault, "imports/nested/a.md", b"import", import_destination="imports").status == "published"
    assert (vault.path / "imports/nested/a.md").read_bytes() == b"import"
    assert publish(vault, "imports2/a.md", b"import", import_destination="imports").status == "rejected"
    assert publish(vault, f"{INBOX}/other.md", b"import", import_destination="imports").status == "rejected"


def test_policy_can_only_narrow_approved_writes(vault):
    (vault.path / "BRAIN.md").write_bytes(b"javis/inbox/allowed\r\nimports/allowed\r\n")
    assert publish(vault, NOTE, b"note").status == "rejected"
    assert publish(vault, f"{INBOX}/allowed/note.md", b"note").status == "published"
    assert publish(vault, "imports/allowed/a.md", b"import").status == "rejected"
    assert publish(vault, "imports/allowed/a.md", b"import", import_destination="imports").status == "published"
    assert publish(vault, "imports/other/a.md", b"import", import_destination="imports").status == "rejected"


@pytest.mark.parametrize("policy", [
    b"javis/inbox\n../elsewhere", b"\xff", b"", b"exclude: elsewhere",
    pytest.param(b"javis/inbox\n".ljust(65537, b"\n"), id="oversize"),
])
def test_invalid_or_empty_policy_denies_notes_and_imports(vault, policy):
    (vault.path / "BRAIN.md").write_bytes(policy)
    assert publish(vault, NOTE, b"note").status == "rejected"
    assert publish(vault, "imports/note.md", b"import", import_destination="imports").status == "rejected"
    assert list((vault.path / INBOX).iterdir()) == []
    assert not (vault.path / "imports").exists()


def test_policy_absence_must_be_confirmed(vault, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", denied)
        result = publish(vault, NOTE, b"note")
    assert result.status == "rejected"
    assert "BRAIN.md" in result.reason
    assert list((vault.path / INBOX).iterdir()) == []


def test_policy_must_be_a_regular_file(vault):
    (vault.path / "BRAIN.md").mkdir()
    assert publish(vault, NOTE, b"note").status == "rejected"
    assert list((vault.path / INBOX).iterdir()) == []


def test_replaced_vault_root_is_rejected(vault):
    old = vault.path.rename(vault.path.with_name("old"))
    (vault.path / INBOX).mkdir(parents=True)

    assert publish(vault, NOTE, b"note").status == "rejected"
    assert list((vault.path / INBOX).iterdir()) == []
    assert list((old / INBOX).iterdir()) == []


def test_publication_error_removes_temporary_file(vault, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "publication failed")

    monkeypatch.setattr(os, "rename" if os.name == "nt" else "link", fail)
    assert publish(vault, NOTE, b"note").status == "failed"
    assert list((vault.path / INBOX).iterdir()) == []


def test_cleanup_preserves_a_replacement_at_the_temporary_path(vault, monkeypatch):
    directory = vault.path / INBOX
    relocated = vault.path / "relocated.tmp"

    def substitute(parent, temporary, name):
        path = directory / temporary
        path.rename(relocated)
        path.write_bytes(b"unrelated file")
        raise OSError(errno.EIO, "publication failed")

    monkeypatch.setattr(publishing, "_publish", substitute)
    assert publish(vault, NOTE, b"note").status == "failed"
    [replacement] = directory.iterdir()
    assert replacement.read_bytes() == b"unrelated file"
    assert relocated.read_bytes() == b"note"


def test_file_created_during_publication_is_preserved(vault, monkeypatch):
    primitive = "rename" if os.name == "nt" else "link"
    original = getattr(os, primitive)

    def race(*args, **kwargs):
        (vault.path / NOTE).write_bytes(b"concurrent creator")
        return original(*args, **kwargs)

    monkeypatch.setattr(os, primitive, race)
    assert publish(vault, NOTE, b"note").status == "conflict"
    assert list((vault.path / INBOX).iterdir()) == [vault.path / NOTE]
    assert (vault.path / NOTE).read_bytes() == b"concurrent creator"


def test_published_inode_mismatch_leaves_target_alone(vault, monkeypatch):
    def substitute(*args, **kwargs):
        (vault.path / NOTE).write_bytes(b"different file")

    monkeypatch.setattr(publishing, "_publish", substitute)
    result = publish(vault, NOTE, b"note")
    assert result.status == "failed"
    assert result.reason == "published file could not be verified"
    assert list((vault.path / INBOX).iterdir()) == [vault.path / NOTE]
    assert (vault.path / NOTE).read_bytes() == b"different file"


def test_exclusion_overrides_grants_for_all_spellings(vault):
    (vault.path / "BRAIN.md").write_text(
        f"exclude: {INBOX}/보관\nexclude: {INBOX}/Private\n{INBOX}\n{INBOX}/보관/child",
        encoding="utf-8",
    )
    for directory in ("보관", unicodedata.normalize("NFD", "보관"), "PRIVATE"):
        assert publish(vault, f"{INBOX}/{directory}/child/a.md", b"note").status == "rejected"
        assert not (vault.path / INBOX / directory).exists()
    assert publish(vault, f"{INBOX}/보관함/a.md", b"note").status == "published"


def test_policy_size_boundary_and_ambiguous_filename(vault):
    policy = vault.path / "BRAIN.md"
    policy.write_bytes(b"javis/inbox\n".ljust(65536, b"\n"))
    assert publish(vault, NOTE, b"note").status == "published"
    policy.rename(vault.path / "brain.MD")
    assert publish(vault, f"{INBOX}/another.md", b"note").status == "rejected"


@pytest.mark.parametrize("kind", ["note.create", "brain.import"])
def test_approval_rechecks_excluded_destination(vault, monkeypatch, kind):
    data = vault.path.parent / "data"
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(vault.path))
    monkeypatch.setenv("JAVIS_DATA_DIR", str(data))
    if kind == "brain.import":
        source = vault.path.parent / "source"
        source.mkdir()
        (source / "a.md").write_bytes(b"note")
        assert cli.main(["brain", "import", str(source), "--dest", INBOX]) == 0
    else:
        assert cli.main(["ask", "/note example"]) == 0
    store = Store.open(data)
    [draft] = store.list_approvals("pending")
    assert draft.kind == kind
    (vault.path / "BRAIN.md").write_text(f"{INBOX}\nexclude: {INBOX}", encoding="utf-8")
    assert cli.main(["approve", draft.id]) == 1
    assert store.get_approval(draft.id).status == "failed"
    assert list((vault.path / INBOX).iterdir()) == []
