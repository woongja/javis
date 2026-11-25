"""Build the bounded model input and keep its source identifiers local."""

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import cached_property
from itertools import chain

import brain
from brain.context import Candidate
from worklog import Store
from worklog.search import RecordExcerpt

_PRIORITY = {"title": 0, "paragraph": 1, "record": 2, "neighbor": 3}
_MAX_REFERENCES = 6
_MAX_EXCERPT_BYTES = 1024
_MAX_BYTES = 4096


@dataclass
class Context:
    payload: str
    sources: dict[str, str | int]  # Temporary ID to vault-relative path or worklog ID.


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _rank(hit: Candidate | RecordExcerpt) -> tuple[int, int]:
    kind = hit.kind if isinstance(hit, Candidate) else "record"
    return -hit.matched_terms, _PRIORITY[kind]


@dataclass
class _Passage:
    kind: str
    source: str | int
    excerpt: str
    hashes: set[bytes]

    def reference(self, number: int) -> dict[str, str]:
        prefix, source_type = ("R", "record") if self.kind == "record" else ("N", "note")
        return {"id": f"{prefix}{number}", "type": source_type, "excerpt": self.excerpt}

    @cached_property
    def size(self) -> int:
        # Every possible temporary ID has one digit.
        return len(_json(self.reference(1)).encode("utf-8"))


def _clip(text: str, start: int = 0) -> str:
    raw = text[start:start + _MAX_EXCERPT_BYTES].encode("utf-8")
    return raw[:_MAX_EXCERPT_BYTES].decode("utf-8", errors="ignore")


def _passage(hit: Candidate | RecordExcerpt, terms: re.Pattern[str]) -> _Passage:
    if isinstance(hit, Candidate):
        kind, source = hit.kind, hit.relative_path
    else:
        kind, source = "record", hit.id
    text = hit.excerpt
    excerpt = _clip(text)
    if len(excerpt) < len(text) and not terms.search(excerpt):
        # A long sentence can have its only matching word beyond the byte cutoff.
        if match := terms.search(text):
            excerpt = _clip(text, match.start())
    hashes = {hashlib.sha256(" ".join(body.split()).encode("utf-8")).digest() for body in {text, excerpt}}
    return _Passage(kind, source, excerpt, hashes)


def _reserved(eligible: Iterable[_Passage], records: list[_Passage], room: int) -> tuple[_Passage, ...]:
    if records:
        for note in eligible:
            if note.kind not in ("title", "paragraph"):
                continue
            for record in records:
                if note.size + 1 + record.size <= room and note.hashes.isdisjoint(record.hashes):
                    return note, record
    return ()


def _select(question: str, terms: tuple[str, ...], notes: list[Candidate], records: list[RecordExcerpt]) -> Context:
    # The stable shared sort preserves each source's own tie breakers.
    notes = sorted(notes, key=lambda n: (n.relative_path, n.paragraph))
    records = sorted(records, key=lambda r: (r.created_utc, r.id), reverse=True)
    ranked = sorted(chain(notes, records), key=_rank)
    pattern = re.compile("|".join(map(re.escape, terms)), re.IGNORECASE)
    record_slots = {hit.id: _passage(hit, pattern) for hit in ranked if isinstance(hit, RecordExcerpt)}

    def passages() -> Iterator[_Passage]:
        for hit in ranked:
            passage = record_slots[hit.id] if isinstance(hit, RecordExcerpt) else _passage(hit, pattern)
            if passage.excerpt.strip():
                yield passage

    references = []
    sources = {}
    hashes = set()
    # The question is preserved verbatim. The byte budget covers the references block.
    room = _MAX_BYTES - len(_json({"references": []}).encode("utf-8"))
    fitting_records = [p for p in record_slots.values() if p.excerpt.strip() and p.size <= room]
    reserved = _reserved(passages(), fitting_records, room)
    for passage in chain(reserved, passages()):
        if passage.source in sources.values() or not passage.hashes.isdisjoint(hashes):
            continue
        separator_bytes = 1 if references else 0
        if passage.size + separator_bytes > room:
            continue
        source_type = "record" if passage.kind == "record" else "note"
        number = 1 + sum(ref["type"] == source_type for ref in references)
        ref = passage.reference(number)
        references.append(ref)
        sources[ref["id"]] = passage.source
        hashes.update(passage.hashes)
        room = _MAX_BYTES - len(_json({"references": references}).encode("utf-8"))
        if len(references) == _MAX_REFERENCES:
            break
    return Context(_json({"question": question, "references": references}), sources)


def build_context(question: str, store: Store | None) -> Context:
    """Collect both sources together. An unavailable source discards all references."""
    terms = brain.search_terms(question)
    if terms and store is not None:
        try:
            notes = brain.context_candidates(question)
            records = store.context_excerpts(terms)
            if records is not None:
                return _select(question, terms, notes, records)
        except (brain.Unavailable, OSError, sqlite3.Error, UnicodeError):
            pass
    return Context(_json({"question": question, "references": []}), {})
