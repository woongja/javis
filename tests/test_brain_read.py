from pathlib import Path
from types import SimpleNamespace

import pytest

import brain
from teams import LocalOnly, registry


@pytest.fixture
def vault(tmp_path, monkeypatch):
    directory = tmp_path / "vault"
    directory.mkdir()
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(directory))
    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path / "data"))
    return directory


def test_title_priority_path_and_paragraph_order_and_result_limit(vault):
    (vault / "nested").mkdir()
    (vault / "nested" / "z.md").write_text("# STRASSE Road\n\nSecond title source", encoding="utf-8")
    (vault / "z Straße Road.md").write_text("# Different heading", encoding="utf-8")
    (vault / "a.md").write_text(
        "Road only\n\nStraße only\n\nStraße Road first\n\nStraße Road second",
        encoding="utf-8",
    )
    (vault / "b.md").write_text("Straße Road third\n\nStraße Road fourth", encoding="utf-8")

    result = brain.search("  straße\tROAD ")
    assert result.matches == (
        brain.Match("nested/z.md", "STRASSE Road"),
        brain.Match("z Straße Road.md", "z Straße Road"),
        brain.Match("a.md", "Straße Road first"),
        brain.Match("a.md", "Straße Road second"),
        brain.Match("b.md", "Straße Road third"),
    )
    assert result.omitted and result.read_failures == 0


def test_file_size_excerpt_and_scan_limits(vault, monkeypatch):
    (vault / "a.md").write_text("target " + "x" * brain.MAX_EXCERPT_CHARS, encoding="utf-8")
    (vault / "b.md").write_bytes(b"target" + b"\xff" * brain.MAX_FILE_BYTES)
    result = brain.search("target")
    [match] = result.matches
    assert match.relative_path == "a.md"
    assert len(match.excerpt) == brain.MAX_EXCERPT_CHARS and match.excerpt.endswith("…")
    assert result.omitted and result.read_failures == 0

    monkeypatch.setattr(brain, "MAX_SCAN_ENTRIES", 1)
    assert brain.search("target").omitted
    assert "생략됨" in registry(None)["/brain"].handler("target", "cli").text


def test_hidden_and_non_markdown_entries_are_excluded(vault):
    (vault / ".hidden").mkdir()
    (vault / ".hidden" / "note.md").write_text("target", encoding="utf-8")
    (vault / ".hidden.md").write_text("target", encoding="utf-8")
    (vault / "note.txt").write_text("target", encoding="utf-8")
    (vault / "visible.MD").write_text("# target", encoding="utf-8-sig")
    assert brain.search("target") == brain.SearchResult((brain.Match("visible.MD", "target"),), 0, False)


def test_linked_file_and_directory_are_excluded(vault, monkeypatch):
    outside = vault.parent / "outside"
    outside.mkdir()
    note = outside / "note.md"
    note.write_text("target", encoding="utf-8")
    try:
        (vault / "file.md").symlink_to(note)
        (vault / "directory").symlink_to(outside, target_is_directory=True)
        linked_root = vault.parent / "linked-vault"
        linked_root.symlink_to(vault, target_is_directory=True)
    except OSError as error:
        if brain.os.name == "nt" and error.winerror in (5, 1314):
            pytest.skip("Windows symlink permission unavailable")
        raise
    assert brain.search("target") == brain.SearchResult((), 0, False)
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(linked_root))
    assert brain.available()[0] is False


def test_reparse_points_are_excluded(vault, monkeypatch):
    junction = vault / "junction"
    junction.mkdir()
    (junction / "note.md").write_text("target", encoding="utf-8")
    original = Path.lstat

    def lstat(path):
        info = original(path)
        if path == junction:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=brain.stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    assert brain.search("target") == brain.SearchResult((), 0, False)


def test_no_matches_and_decode_failure_have_distinct_status(vault):
    team = registry(None)["/brain"]
    assert team.handler("absent", "cli") == LocalOnly("검색 범위에서 일치하는 노트가 없습니다.")
    (vault / "invalid.md").write_bytes(b"\xff")
    assert brain.search("absent") == brain.SearchResult((), 1, False)
    assert team.handler("absent", "cli") == LocalOnly("읽기 실패 1건.", "error")


def test_environment_precedes_local_setting(vault, monkeypatch):
    def setting(checkout, name, *, env):
        assert (checkout / ".git").exists()
        assert name == "JAVIS_BRAIN_DIR" and env == {}
        return str(vault)

    with monkeypatch.context() as patch:
        patch.setattr(brain, "env_value", lambda *args, **kwargs: pytest.fail("unexpected fallback"))
        assert brain.available() == (True, "")
    monkeypatch.delenv("JAVIS_BRAIN_DIR")
    monkeypatch.setattr(brain, "env_value", setting)
    assert brain.available() == (True, "")


@pytest.mark.parametrize("relation", ["repo", "in repo", "above repo", "data", "in data", "above data"])
def test_overlapping_roots_are_rejected(vault, monkeypatch, relation):
    checkout = Path(__file__).resolve().parents[1]
    data = vault.parent / "data"
    configured = {
        "repo": checkout,
        "in repo": checkout / "unused",
        "above repo": checkout.parent,
        "data": data,
        "in data": data / "nested",
        "above data": data.parent,
    }[relation]
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(configured))
    enabled, reason = brain.available()
    assert not enabled and "겹칠 수 없습니다" in reason and str(configured) not in reason


def test_missing_directory_is_unavailable(vault, monkeypatch):
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(vault / "missing"))
    enabled, reason = brain.available()
    assert not enabled and "접근할 수 없습니다" in reason and str(vault) not in reason
