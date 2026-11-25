import io
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import pytest

import brain
from brain import context


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(root))
    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path / "data"))

    def write(path, content):
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")
        return file

    return write


def neighbors(question):
    return [c for c in brain.context_candidates(question) if c.kind == "neighbor"]


_ATOMIC = """---
type: atomic
tags: ["전시", "점검"]
source: "[[javis/wiki/notes/전시 준비]]"
derived_sources:
  - "[[javis/wiki/notes/전시 준비]]"
  - "[[me/모형 전시]]"
---
"""
_EXAMPLE = """# 전시 점검
모형 전시를 열기 전에 조명을 점검한다.

- 사실: 조명 점검은 개장 전날에 한다.
- 사실: 점검 장소는 모형실이다.
- 관계: 점검 대상 [[javis/wiki/notes/전시 조명]]
"""
_EMPTY_SOURCES = '---\ntype: atomic\ntags: []\nsource: "conversation"\nderived_sources: []\n---\n'


@pytest.mark.parametrize("extra", [False, True])
def test_only_first_three_body_relation_links_are_resolved(vault, monkeypatch, extra):
    prefix = "javis/wiki/notes/"
    body = _EXAMPLE
    expected = ["전시 조명"]
    if extra:
        body = body.replace("# 전시 점검", "# 전시 점검 [[전시 준비]]")
        body = body.replace("모형실이다.", "모형실이다. [[me/모형 전시]]")
        body += "- 관계: [[배선|도면]] [[램프#규격]]\n- 관계: [[예비품]]"
        expected += ["배선", "램프"]
    vault(prefix + "전시 점검.md", _ATOMIC + body)
    vault("me/모형 전시.md", "원본")
    for title in ["전시 준비", "전시 조명", "배선", "램프", "예비품"]:
        vault(prefix + title + ".md", _EMPTY_SOURCES + f"# {title}\n요약\n\n- 사실: 첫 사실\n- 사실: 둘째 사실")

    # Count actual target lookups. Checking whether an alias is safe to display
    # in an excerpt must never fetch a linked note.
    resolved = []

    class Targets(dict):
        def get(self, key):
            resolved.append(key)
            return super().get(key)

    original = context._catalog

    def catalog():
        bodies, targets = original()
        return bodies, Targets(targets)

    monkeypatch.setattr(context, "_catalog", catalog)
    found = neighbors("점검 알려줘")
    assert resolved == [prefix + "전시 조명", *expected[1:]]
    assert [(c.relative_path, c.excerpt, c.matched_terms) for c in found] == [
        (prefix + title + ".md", "- 사실: 첫 사실", 0) for title in expected
    ]


def test_terms_counts_positions_and_order(vault, monkeypatch):
    assert brain.search_terms("언제? Straße,STRASSE/전시_점검! 알려줘 " + "가" * 80 + " A: B C") == (
        "strasse", "전시", "점검", "가" * 64, "a", "b",
    )
    with monkeypatch.context() as patch:
        patch.setattr(context, "_root", lambda: pytest.fail("empty query opened vault"))
        assert brain.context_candidates("언제? 알려줘!") == []
    vault("b.md", "# Road\n\nrain rain\n\nrain on ROAD\n- 관계: [[c]]")
    vault("a.md", "# rain\n\nroad")
    vault("c.md", "one fact\n\nanother fact\n- 관계: [[a]]")
    found = brain.context_candidates("ROAD/rain")
    assert [(c.kind, c.excerpt, c.matched_terms) for c in found] == [
        ("title", "rain", 1), ("title", "Road", 1), ("paragraph", "road", 1),
        ("paragraph", "rain rain", 1), ("paragraph", "rain on ROAD", 2), ("neighbor", "one fact", 0),
    ]
    assert found[3].paragraph < found[4].paragraph


def test_five_distinct_best_parents(vault):
    for i in range(7):
        title = "red blue" if i == 6 else "red"
        vault(f"{i}.md", f"# {title}\n\nred\n\nred\n- 관계: [[n{i}]]")
        vault(f"n{i}.md", "neighbor")
    assert [c.relative_path for c in neighbors("red blue")] == [f"n{i}.md" for i in (6, 0, 1, 2, 3)]


def test_cycles_ambiguous_broken_and_non_relation_links(vault):
    vault("one/dup.md", "- 사실: first")
    vault("two/dup.MD", "# duplicate\n~~~\n- 사실: code\n~~~\n- 사실: second\n- 관계: [[b]] [[end]]")
    vault("end.md", "too far")
    vault("a.md", "# key\n- 관계: [[dup]] [[missing]] [[a]]")
    vault("b.md", "# key\n~~~text\n- 관계: [[end]]\n~~~\n"
          "    - 관계: [[end]]\n- 관계: ![[end]] [[https://invalid.test/n]] https://invalid.test/[[end]] "
          "`[[end]]` ``[[end]]`` [[two/dup.MD#part|label]]")
    assert [(c.relative_path, c.excerpt) for c in neighbors("key")] == [("two/dup.MD", "- 사실: second")]


def test_prunes_exclusions_and_navigation_and_redacts_links(vault, monkeypatch):
    root = vault("BRAIN.md", "exclude: ME/archive").parent
    forbidden = [root / "me/Archive", root / "javis/wiki/topics"]
    vault("me/Archive/source.md", "key")
    vault("javis/wiki/topics/topic.md", "key")
    vault("javis/wiki/index.md", "key")
    vault("javis/wiki/notes/derived.md", _ATOMIC.replace("me/모형 전시", "me/Archive/source") + "# key")
    vault("javis/inbox/copy.md", _ATOMIC.replace("me/모형 전시", "me/Archive/source") + "# key")
    vault("a/twin.md", "x")
    vault("b/twin.md", "y")
    vault("ok.md", "ok")
    vault("key.md", "key [[me/Archive/source|marker]] [[twin|marker]] [[derived|marker]] [[ok|safe]]\n"
                    "- 관계: [[derived]] [[topic]] [[index]]")
    scandir = brain.os.scandir

    def checked(directory):
        assert not any(Path(directory).is_relative_to(path) for path in forbidden)
        return scandir(directory)

    monkeypatch.setattr(brain.os, "scandir", checked)
    found = brain.context_candidates("key")
    assert [(c.relative_path, c.excerpt) for c in found] == [("key.md", "key"), ("key.md", "key safe")]


@pytest.mark.parametrize("header", [
    "", _ATOMIC.replace("derived_sources:", "unused:"), _ATOMIC.replace("me/모형 전시", "../source"),
    _ATOMIC.replace("me/모형 전시", "me/source|alias"), _ATOMIC.replace("me/모형 전시", "me/source.md"),
    _ATOMIC.replace("derived_sources:", "source: conversation\nderived_sources:"),
    _ATOMIC.replace("source: ", "source: bad\nignored: "), _EMPTY_SOURCES.replace("derived_sources: []", "derived_sources:"),
    _EMPTY_SOURCES.replace('"conversation"', '"[[me/absent]]"'),
])
def test_rejects_unverifiable_managed_sources(vault, header):
    vault("javis/wiki/notes/key.md", header + "# key\n- 사실: key")
    assert brain.context_candidates("key") == []


def test_quoted_flow_and_block_source_lists(vault):
    path = "javis/wiki/notes/key.md"
    for sources in ["derived_sources: ['[[me/source]]']", 'derived_sources:\n- "[[me/source]]"']:
        vault(path, f'---\ntype: atomic\ntags: [전시]\nsource: "[[me/source]]"\n{sources}\n---\n# key')
        assert [c.relative_path for c in brain.context_candidates("key")] == [path]


def test_failed_scan_discards_partial_candidates(vault):
    vault("a.md", "key")
    vault("z.md", "").write_bytes(b"\xff")
    with pytest.raises(brain.Unavailable):
        brain.context_candidates("key")
    vault("BRAIN.md", "exclude: ../unsafe")
    with pytest.raises(brain.Unavailable):
        brain.context_candidates("key")


def test_large_vault_counts_actual_file_bytes_and_expands_only_one_hop(vault, monkeypatch):
    vault("notes/key.md", "- 관계: [[notes/first]]")
    vault("notes/first.md", "- 사실: first\n- 관계: [[notes/end]]")
    vault("notes/end.md", "- 사실: end")
    vault("notes/oversize.md", "").write_bytes(b" " * (brain.MAX_FILE_BYTES + 1))
    for i in range(18):
        vault(f"padding/{i}.md", "").write_bytes(b" " * brain.MAX_FILE_BYTES)
    reads = Counter()
    opens = Counter()

    class CountedFile(io.FileIO):
        def read(self, size=-1):
            data = super().read(size)
            reads[self.name] += len(data)
            return data

    def counted_open(path, mode, *, buffering, opener):
        opens[path] += 1
        return CountedFile(path, mode, opener=opener)

    monkeypatch.setattr(brain, "open", counted_open, raising=False)
    assert [c.relative_path for c in neighbors("key")] == ["notes/first.md"]
    assert 15 * 1024**2 <= sum(reads.values()) <= 16 * 1024**2
    assert max(reads.values()) <= 1024**2 and max(opens.values()) == 1
    assert all(path.name != "oversize.md" for path in opens)


def test_cut_walk_counts_entries_and_disables_short_links(vault, monkeypatch):
    vault("a/key.md", "- 관계: [[short]] [[a/qualified]]")
    vault("a/short.md", "ambiguous beyond cutoff")
    vault("a/qualified.md", "reachable")
    for i in range(20):
        vault(f"z/{i}.txt", "")
    vault("z/short.md", "other")
    original = brain.os.scandir
    visits = []

    @contextmanager
    def counted(path):
        with original(path) as entries:
            def items():
                for entry in entries:
                    visits.append(entry.name)
                    yield entry
            yield items()

    monkeypatch.setattr(brain.os, "scandir", counted)
    monkeypatch.setattr(brain, "MAX_SCAN_ENTRIES", 8)
    assert [c.relative_path for c in neighbors("key")] == ["a/qualified.md"]
    assert len(visits) == 8
