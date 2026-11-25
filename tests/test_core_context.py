import json
from dataclasses import replace

import pytest

import brain
from brain.context import Candidate
from core.context import build_context
from tests.test_worklog_store import SAMPLE
from worklog import Item, Store
from worklog import search


@pytest.fixture
def local(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    data = tmp_path / "data"
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(vault))
    monkeypatch.setenv("JAVIS_DATA_DIR", str(data))
    return vault, Store.open(data)


def test_reserves_both_sources_and_fills_by_rank(local):
    vault, store = local
    for name in "gfedcba":
        (vault / f"{name}.md").write_text(f"# 빛 색 {name}\n\n빛 색 다른 문단", encoding="utf-8")
    record_id = store.save(replace(SAMPLE, request="빛", items=(Item("측정했다. 빛 확인.", ()),)))
    question = "빛, 색? 알려줘"
    result = build_context(question, store)
    assert list(result.sources.values()) == ["a.md", record_id, "b.md", "c.md", "d.md", "e.md"]
    payload = json.loads(result.payload)
    assert payload["question"] == question
    assert payload["references"][:2] == [
        {"id": "N1", "type": "note", "excerpt": "빛 색 a"},
        {"id": "R1", "type": "record", "excerpt": "빛 확인."},
    ]
    assert list(result.sources) == [r["id"] for r in payload["references"]]
    assert ".md" not in result.payload


@pytest.mark.parametrize("term", ["AI", "É"])
def test_short_context_terms_match_case_in_answers_and_questions(local, term):
    _, store = local
    answer_id = store.save(replace(SAMPLE, request="진행 상황", items=(Item(f"확인 완료. {term} 준비 중.", ()),)))
    question_id = store.save(replace(SAMPLE, request=f"{term} 담당은?", items=()))

    result = build_context(f"{term} 일정", store)
    excerpts = {result.sources[ref["id"]]: ref["excerpt"] for ref in json.loads(result.payload)["references"]}
    assert excerpts == {answer_id: f"{term} 준비 중.", question_id: f"{term} 담당은?"}
    assert store.search(term.lower()) == ("ok", [])
    assert [hit.id for hit in store.search(term)[1]] == [question_id, answer_id]


def test_distinct_terms_kind_position_and_utc_id_ties(local):
    vault, store = local
    (vault / "z.md").write_text("# 빛 제목\n\n빛 색 문단\n\n색 빛 둘째", encoding="utf-8")
    (vault / "b.md").write_text("빛 문단\n- 관계: [[이웃]]", encoding="utf-8")
    (vault / "a.md").write_text("# 색 제목", encoding="utf-8")
    (vault / "이웃.md").write_text("이웃 사실", encoding="utf-8")
    older = store.save(replace(SAMPLE, request="빛 색", items=(Item("빛 색 older", ()),)))
    newest = store.save(replace(SAMPLE, request="빛 색", created_at="2026-11-22T00:00:00Z"))
    tied = store.save(replace(SAMPLE, request="빛 색 tied"))
    store.save(replace(SAMPLE, request="색    제목"))  # Whitespace duplicate across sources.
    result = build_context("빛 색", store)
    assert list(result.sources.values()) == ["z.md", newest, tied, older, "a.md", "b.md"]
    assert json.loads(result.payload)["references"][0]["excerpt"] == "빛 색 문단"


def test_reservation_can_skip_a_duplicate_and_keep_a_neighbor(local):
    vault, store = local
    (vault / "a.md").write_text("# 빛 중복", encoding="utf-8")
    (vault / "b.md").write_text("빛 문단\n- 관계: [[missing]] [[n]]", encoding="utf-8")
    (vault / "n.md").write_text("이웃 사실", encoding="utf-8")
    record_id = store.save(replace(SAMPLE, request="빛   중복"))
    result = build_context("빛", store)
    assert list(result.sources.values()) == ["b.md", record_id, "n.md"]


def test_byte_limits_after_escaping_and_skip_to_a_smaller_excerpt(local, monkeypatch):
    _, store = local
    notes = [Candidate("title", f"{i}.md", f'{i}"\n\\' * 300, 1, 0) for i in range(3)]
    notes += [Candidate("paragraph", "3.md", "가" * 400, 1, 0), Candidate("neighbor", "4.md", "끝", 0, 0)]
    monkeypatch.setattr(brain, "context_candidates", lambda _: notes)
    question = '빛 "언제"\n'
    result = build_context(question, store)
    payload = json.loads(result.payload)
    assert payload["question"] == question
    assert list(result.sources.values()) == ["0.md", "1.md", "4.md"]
    assert len(result.payload.encode("utf-8")) <= 4096
    assert payload["references"][0]["excerpt"] == notes[0].excerpt[:1024]
    monkeypatch.setattr(brain, "context_candidates", lambda _: notes[-2:])
    assert json.loads(build_context(question, store).payload)["references"][0]["excerpt"] == "가" * 341


def test_long_question_and_late_record_match(local):
    _, store = local
    body = "가" * 500 + " 점검 " + "나" * 500
    record_id = store.save(replace(SAMPLE, items=(Item(body, ()),)))
    question = "점검 " + "언제? " * 1200
    result = build_context(question, store)
    payload = json.loads(result.payload)
    assert payload["question"] == question
    assert result.sources == {"R1": record_id}
    excerpt = payload["references"][0]["excerpt"]
    assert "점검" in excerpt and len(excerpt.encode("utf-8")) <= 1024


def test_exact_serialized_reference_budget(local, monkeypatch):
    _, store = local
    question = '빛?\n"'
    excerpts = [f'{i}빛"\n' + "가" * 337 for i in range(3)] + ["빛"]
    notes = [Candidate("title", f"{i}.md", s, 1, 0) for i, s in enumerate(excerpts)]
    monkeypatch.setattr(brain, "context_candidates", lambda _: notes)

    def reference_block():
        refs = json.loads(build_context(question, store).payload)["references"]
        return json.dumps({"references": refs}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    remaining = 4096 - len(reference_block())
    notes[-1] = replace(notes[-1], excerpt="빛" + "x" * remaining)
    assert len(reference_block()) == 4096
    notes[-1] = replace(notes[-1], excerpt=notes[-1].excerpt + "x")
    assert len(json.loads(reference_block())["references"]) == 3


@pytest.mark.parametrize("failure", ["missing", "unicode61", "db_error", "index_state", "policy", "no_store"])
def test_failed_source_discards_all_context(local, monkeypatch, failure):
    vault, store = local
    (vault / "key.md").write_text("evidence", encoding="utf-8")
    store.save(replace(SAMPLE, request="key"))
    if failure in ("missing", "unicode61"):
        monkeypatch.setattr(search, "_detect_tokenizer", lambda db: None if failure == "missing" else failure)
        store = Store.open(vault.parent / "data")
    elif failure == "db_error":
        with store._connect() as db:
            db.execute("DROP TABLE search_trigram")
    elif failure == "index_state":
        with store._connect() as db:
            db.execute("DELETE FROM search_index")
    elif failure == "policy":
        (vault / "BRAIN.md").write_text("exclude: ../invalid", encoding="utf-8")
    else:
        store = None
    result = build_context("key?", store)
    assert json.loads(result.payload) == {"question": "key?", "references": []}
    assert result.sources == {}
