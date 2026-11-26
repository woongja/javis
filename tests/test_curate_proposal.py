import builtins
import codecs
import hashlib
import io
import json
import os
import unicodedata
from dataclasses import replace

import pytest

from brain.curate import (
    NOTES, TOPICS, AtomicNote, Catalog, Names, note_link, parse_note, read_catalog,
    serialize_note, validated_title,
)
from brain.publish import pin_root
from core.curate import build_prompt, build_proposal, preview


EXAMPLE = """---
type: atomic
tags: ["전시", "점검"]
source: "[[javis/wiki/notes/전시 준비]]"
derived_sources:
  - "[[javis/wiki/notes/전시 준비]]"
  - "[[me/모형 전시]]"
---
# 전시 점검
모형 전시를 열기 전에 조명을 점검한다.

- 사실: 조명 점검은 개장 전날에 한다.
- 사실: 점검 장소는 모형실이다.
- 관계: 점검 대상 [[javis/wiki/notes/전시 조명]]
""".encode()
EXISTING = NOTES + "전시 점검.md"
EMPTY = Catalog((1, 2), None, "absent", (), True, {})
NOTE = AtomicNote("주제", "요약", ("사실",), (), (), "conversation", ())


def create(id="P1", title="주제", **fields):
    return {"id": id, "title": title, "summary": "요약", "tags": [], "facts": ["첫 사실"],
            "relations": [], "sources": ["conversation"], **fields}


def append(id="P1", **fields):
    return {"id": id, "target": "N1", "facts": ["추가 사실"], "relations": [],
            "sources": ["conversation"], **fields}


def write(root, path, data):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def prepare(root, *paths, records=()):
    catalog = read_catalog(pin_root(root), set(paths))
    ranges = []
    for path in paths:
        data = catalog.selected[path].content
        start = data.find("- 사실:".encode()) if path.startswith(NOTES) else 0
        end = data.find(b"\n", start)
        ranges.append((path, start, end + 1 if end >= 0 else len(data)))
    return build_prompt("이번 입력", catalog, ranges=tuple(ranges), records=records)


def test_design_example_round_trip():
    note = parse_note(EXAMPLE, title="전시 점검")
    assert note.tags == ("전시", "점검")
    assert note.facts[-1] == "점검 장소는 모형실이다."
    assert note.relations == ("점검 대상 [[javis/wiki/notes/전시 조명]]",)
    assert serialize_note(note) == EXAMPLE
    with pytest.raises(ValueError):
        parse_note(EXAMPLE, title="다른 제목")


@pytest.mark.parametrize("old,new", [
    ('tags: ["전시", "점검"]', 'tags: "전시"'),
    ("type: atomic", "type: atomic\ntype: atomic"),
    ("type: atomic", "type: other"),
    ("derived_sources:", "extra: value\nderived_sources:"),
    ('source: "[[javis/wiki/notes/전시 준비]]"', 'source: "record:0"'),
    ('source: "[[javis/wiki/notes/전시 준비]]"', 'source: "[[missing]]"'),
    ("[[me/모형 전시]]", "[[me/모형 전시|별칭]]"),
])
def test_reject_malformed_frontmatter(old, new):
    with pytest.raises(ValueError):
        parse_note(EXAMPLE.replace(old.encode(), new.encode()))


def test_note_byte_limit():
    room = 2048 - len(serialize_note(NOTE))
    full = replace(NOTE, summary=NOTE.summary + "x" * room)
    raw = serialize_note(full)
    assert len(raw) == 2048 and parse_note(raw) == full
    with pytest.raises(ValueError):
        serialize_note(replace(full, summary=full.summary + "x"))
    with pytest.raises(ValueError):
        parse_note(raw + b"\n")


@pytest.mark.parametrize("count", [0, 1, 5, 6])
@pytest.mark.parametrize("field", ["facts", "relations"])
def test_item_limits(field, count):
    items = tuple(f"항목 {i}" if field == "facts" else f"대상 [[주제{i}]]" for i in range(count))
    note = replace(NOTE, **{field: items})
    if count > 5 or (field == "facts" and not count):
        with pytest.raises(ValueError):
            serialize_note(note)
    else:
        assert parse_note(serialize_note(note)) == note


@pytest.mark.parametrize("title", ["", " x", "x ", "x.y", "x\n", *'<>:"/\\|?*',
                                   "cOn", "pRn", "AUX", "nul", "COM1", "lPt9", "COM¹",
                                   "LPT²", "COM³", "Brain", "agents", "CLAUDE", "a~1"])
def test_rejected_titles(title):
    with pytest.raises(ValueError):
        validated_title(title)


def test_title_byte_limit_including_suffix():
    assert validated_title("한글 제목_1-A") == "한글 제목_1-A"
    full = "글" * 39
    assert len((validated_title(full) + ".md").encode()) == 120
    with pytest.raises(ValueError):
        validated_title(full + "x")
    with pytest.raises(ValueError):
        Names((full + ".md",), True).allocate(full)
    with pytest.raises(ValueError):
        Names((), False).allocate("새 노트")


def test_inheritance_split_and_pinned_source_ranges(tmp_path):
    write(tmp_path, EXISTING, EXAMPLE)
    path = "me/새 자료.md"
    write(tmp_path, path, "표시할 문장\n숨길 문장\n".encode())
    write(tmp_path, "BRAIN.md", b"javis/wiki\r\n")
    prompt = prepare(tmp_path, EXISTING, path, records=((17, "공개 기록 조각"),))
    assert path not in prompt.payload and "숨길 문장" not in prompt.payload
    assert "derived_sources" not in prompt.payload and "record:17" not in prompt.payload
    proposal = build_proposal(prompt, {"ops": [
        create(sources=["N1", "R1"]), append("P2", sources=["N2"]),
    ]})
    spec = json.loads(proposal.payload)
    assert spec["version"] == 1 and spec["root_id"] == list(prompt.catalog.root_id)
    assert spec["policy_hash"] == hashlib.sha256(b"javis/wiki\r\n").hexdigest()
    assert spec["sources"]["N2"] == {"path": path, "byte_range": [0, len("표시할 문장\n".encode())],
                                     "sha256": hashlib.sha256((tmp_path/path).read_bytes()).hexdigest()}
    assert spec["sources"]["R1"] == {"record_id": 17}
    assert "표시할 문장" not in proposal.payload and "숨길 문장" not in proposal.payload
    first, separate = spec["ops"]
    inherited = {note_link(EXISTING), *parse_note(EXAMPLE).derived_sources}
    assert set(parse_note(first["content"].encode()).derived_sources) == inherited
    assert separate["kind"] == "create" and separate["path"] == NOTES + "전시 점검-2.md"
    split = parse_note(separate["content"].encode())
    assert set(split.derived_sources) == inherited | {note_link(path)}
    assert split.source == note_link(path) and split.facts == ("추가 사실",)
    assert "새 근거로 별도 노트" in proposal.preview and "기록 #17" in proposal.preview


def test_merge_append_pin_original_and_deduplicate(tmp_path, monkeypatch):
    base = codecs.BOM_UTF8 + EXAMPLE.rstrip().replace(b"\n", b"\r\n")
    write(tmp_path, EXISTING, base)
    lamp = NOTES + "전시 조명.md"
    write(tmp_path, lamp, serialize_note(replace(NOTE, title="전시 조명")))
    source = "me/모형 전시.md"
    write(tmp_path, source, b"original")
    rel = {"target": "N2", "label": "같은 링크"}
    with monkeypatch.context() as guard:
        forbid_writes(guard)
        prompt = prepare(tmp_path, EXISTING, lamp, source)
        proposal = build_proposal(prompt, {"ops": [
            append(facts=["점검 장소는 모형실이다.", "추가 사실"], relations=[rel], sources=["N3"]),
            append("P2", facts=["추가 사실", "다음 사실"], conflicts=["점검일 확인"]),
        ]})
    op, = json.loads(proposal.payload)["ops"]
    expected = "\r\n- 사실: 추가 사실\r\n- 사실: 다음 사실\r\n".encode()
    assert op["content"].encode() == expected
    assert op["kind"] == "append" and op["path"] == EXISTING
    assert op["file_id"] == list(prompt.catalog.selected[EXISTING].identity)
    assert op["base_sha256"] == hashlib.sha256(base).hexdigest()
    assert op["result_sha256"] == hashlib.sha256(base + expected).hexdigest()
    assert "기존 노트에 사실·링크 추가 1개" in proposal.preview and "점검일 확인" in proposal.preview
    assert "이번 입력" in proposal.preview and "추가 사실" in proposal.preview
    assert preview(proposal.payload) == proposal.preview


@pytest.mark.parametrize("ops", [
    [create(sources=["N3"])], [append(target="N2")],
    [create(relations=[{"label": "관계", "target": "N5"}])],
    [create(), create(title="중복 ID")], [create(), create("P2")],
    [create(relations=[{"label": "선행 없음", "target": "P2"}]), create("P2", "다음")],
    [create(relations=[{"label": "자기 참조", "target": "P1"}])],
    [create(facts=["직접 삽입 [[대상]]"])], [create(path="임의.md")],
])
def test_reject_invalid_proposals(ops):
    with pytest.raises(ValueError):
        build_proposal(build_prompt("입력", EMPTY), {"ops": ops})


def forbid_writes(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("unexpected filesystem mutation during proposal")

    def checked_open(file, mode="r", *args, **kwargs):
        if any(c in mode for c in "wax+"):
            denied()
        return original_io(file, mode, *args, **kwargs)

    def checked_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_APPEND | os.O_CREAT | os.O_TRUNC | os.O_RDWR | os.O_WRONLY):
            denied()
        return original_open(path, flags, *args, **kwargs)

    original_io, original_open = io.open, os.open
    monkeypatch.setattr(io, "open", checked_open)
    monkeypatch.setattr(builtins, "open", checked_open)
    monkeypatch.setattr(os, "open", checked_os_open)
    for name in ("mkdir", "unlink", "rename", "replace", "rmdir", "link", "symlink"):
        monkeypatch.setattr(os, name, denied)


def test_first_free_name_in_preview_links_and_hashes_without_writes(tmp_path, monkeypatch):
    title = "한글 Model"
    write(tmp_path, TOPICS + unicodedata.normalize("NFD", title.upper()) + ".md", b"topic")
    write(tmp_path, NOTES + title + "-2.md", b"existing")
    write(tmp_path, "other/" + title + "-4.md", b"manual")
    with monkeypatch.context() as guard:
        forbid_writes(guard)
        prompt = prepare(tmp_path)
        proposal = build_proposal(prompt, {"ops": [
            create(title=unicodedata.normalize("NFD", title)),
            create("P2", title.lower(), relations=[{"label": "관련", "target": "P1"}]),
            create("P3", "목록", relations=[{"label": "", "target": "P2"}]),
        ]})
    ops = json.loads(proposal.payload)["ops"]
    for op, name in zip(ops, [title + "-3", title.lower() + "-5", "목록"], strict=True):
        assert op["absent"] is True and op["path"] == NOTES + name + ".md"
        assert op["path"] in proposal.preview and op["content"].rstrip() in proposal.preview
        assert parse_note(op["content"].encode()).title == name
        assert op["result_sha256"] == hashlib.sha256(op["content"].encode()).hexdigest()
    assert ops[1]["depends_on"] == ["P1"] and note_link(ops[0]["path"]) in ops[1]["content"]
    assert ops[2]["depends_on"] == ["P2"] and note_link(ops[1]["path"]) in ops[2]["content"]
    assert proposal.preview.startswith("새 노트 3개 / 기존 노트에 사실·링크 추가 0개 / 목차 0개")


def test_final_note_limits_apply_to_append(tmp_path):
    room = 2048 - len(serialize_note(NOTE)) - len("- 사실: 추가 사실\n".encode())
    full = serialize_note(replace(NOTE, summary=NOTE.summary + "x" * room))
    write(tmp_path, NOTES + "주제.md", full)
    prompt = prepare(tmp_path, NOTES + "주제.md")
    op, = json.loads(build_proposal(prompt, {"ops": [append()]}).payload)["ops"]
    assert len(full + op["content"].encode()) == 2048
    with pytest.raises(ValueError):
        build_proposal(prompt, {"ops": [append(facts=["추가 사실x"])]})
    with pytest.raises(ValueError):
        build_proposal(prompt, {"ops": [append(facts=list("12345"))]})


def test_proposal_budgets_include_serialized_metadata():
    prompt = build_prompt("입력", EMPTY)
    rows = [create(f"P{i}", f"주제{i}") for i in range(1, 18)]
    assert len(json.loads(build_proposal(prompt, {"ops": rows[:16]}).payload)["ops"]) == 16
    with pytest.raises(ValueError):
        build_proposal(prompt, {"ops": rows})
    base = create(conflicts=["확인"])
    size = len(build_proposal(prompt, {"ops": [base]}).payload.encode())
    base["conflicts"][0] += "x" * (65536 - size)
    assert len(build_proposal(prompt, {"ops": [base]}).payload.encode()) == 65536
    base["conflicts"][0] += "x"
    with pytest.raises(ValueError):
        build_proposal(prompt, {"ops": [base]})


def test_exclusions_apply_to_inheritance_and_destinations(tmp_path):
    write(tmp_path, EXISTING, EXAMPLE)
    write(tmp_path, "BRAIN.md", b"exclude: me\n")
    with pytest.raises(ValueError):
        prepare(tmp_path, EXISTING)
    with pytest.raises(ValueError):
        build_proposal(prepare(tmp_path), {"ops": [create()]})


def test_split_requires_a_proposed_fact(tmp_path):
    write(tmp_path, EXISTING, EXAMPLE)
    other = "me/별도 자료.md"
    write(tmp_path, other, b"evidence")
    prompt = prepare(tmp_path, EXISTING, other)
    with pytest.raises(ValueError, match="제안된 사실"):
        build_proposal(prompt, {"ops": [append(facts=[], sources=["N2"],
                                               relations=[{"label": "", "target": "N2"}])]})


def test_only_offered_excerpt_ids_are_visible(tmp_path):
    path = "me/원본.md"
    data = b'---\r\ntags: []\r\n---\r\nfirst [[private/hidden|alias]] last\r\n'
    write(tmp_path, path, data)
    write(tmp_path, EXISTING, EXAMPLE)
    catalog = read_catalog(pin_root(tmp_path), {path, EXISTING})
    start = data.index(b"hidden")
    prompt = build_prompt('문장 "지시"\n다음', catalog, ranges=((path, start, len(data)),))
    assert json.loads(prompt.payload)["references"] == [{"id": "N1", "type": "note", "excerpt": "last"}]
    with pytest.raises(ValueError):
        build_prompt("입력", catalog, ranges=((path, 0, len(data)),))
    with pytest.raises(ValueError):
        build_proposal(prompt, {"ops": [create(sources=["N2"])]})


def test_prompt_reference_budgets():
    with pytest.raises(ValueError):
        build_prompt("입력", EMPTY, records=((1, "x"),) * 7)
    with pytest.raises(ValueError):
        build_prompt("입력", EMPTY, records=((1, "가" * 342),))
    with pytest.raises(ValueError):
        build_prompt("입력", EMPTY, records=tuple((i, '"' * 600) for i in range(1, 5)))
