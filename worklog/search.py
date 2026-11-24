"""Free search over the questions and answer text of successful web requests."""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal


SearchStatus = Literal["ok", "degraded", "unavailable", "usage"]
Tokenizer = Literal["trigram", "unicode61"]


@dataclass(frozen=True)
class SearchResult:
    id: int
    date: str  # UTC calendar date in ISO format.
    preview: str


_TABLES: dict[Tokenizer, str] = {"trigram": "search_trigram", "unicode61": "search_unicode61"}
_PREVIEW_LENGTH = 80
_MAX_RESULTS = 20


def _detect_tokenizer(db: sqlite3.Connection) -> Tokenizer | None:
    for tokenizer, table in _TABLES.items():
        try:
            db.execute(f"CREATE VIRTUAL TABLE temp.{table} USING fts5(text, tokenize='{tokenizer}')")
        except sqlite3.OperationalError as exc:
            if str(exc) == "no such module: fts5":
                return None
            if tokenizer == "trigram" and str(exc) == "no such tokenizer: trigram":
                continue
            raise
        db.execute(f"DROP TABLE temp.{table}")
        return tokenizer
    return None


class SearchIndex:
    def __init__(self, db: sqlite3.Connection):
        self._tokenizer = _detect_tokenizer(db)

    @staticmethod
    def _active_tokenizer(db: sqlite3.Connection) -> str | None:
        return db.execute("SELECT tokenizer FROM search_index WHERE singleton = 1").fetchone()[0]

    def synchronize(self, db: sqlite3.Connection) -> bool:
        """Rebuild on a capability change. The caller holds the write transaction."""
        if self._active_tokenizer(db) == self._tokenizer:
            return False
        if self._tokenizer is not None:
            # An older SQLite may be unable to open the other tokenizer's table.
            table = _TABLES[self._tokenizer]
            db.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} "
                f"USING fts5(question, body, tokenize='{self._tokenizer}')"
            )
            db.execute(f"DELETE FROM {table}")
            rows = db.execute("SELECT id, request, items FROM records WHERE kind = 'ask' AND status = 'ok'")
            db.executemany(
                f"INSERT INTO {table} (rowid, question, body) VALUES (?, ?, ?)",
                ((id, question, "\n".join(text for text, _ in json.loads(items))) for id, question, items in rows),
            )
        db.execute("UPDATE search_index SET tokenizer = ? WHERE singleton = 1", (self._tokenizer,))
        return True

    def add(self, db: sqlite3.Connection, id: int, question: str, bodies: Iterable[str]) -> None:
        if self.synchronize(db) or self._tokenizer is None:
            return  # A rebuild already included the newly inserted record.
        table = _TABLES[self._tokenizer]
        db.execute(
            f"INSERT INTO {table} (rowid, question, body) VALUES (?, ?, ?)",
            (id, question, "\n".join(bodies)),
        )

    @staticmethod
    def remove(db: sqlite3.Connection, ids: str) -> tuple[str, ...]:
        """Remove index copies and return tables that could not be cleaned."""
        unclean = []
        # Tokenizer switches can leave a second index containing copies of the records.
        for table in _TABLES.values():
            if db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone() is None:
                continue
            try:
                removed = db.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT value FROM json_each(?))", (ids,))
                if removed.rowcount:
                    # Merge away FTS5 deletion markers and the old terms they mask.
                    db.execute(f"INSERT INTO {table} ({table}) VALUES ('optimize')")
            except sqlite3.OperationalError:
                if not db.in_transaction:
                    raise
                # An older runtime may lack FTS5 or the tokenizer used to create this table.
                unclean.append(table)
        return tuple(unclean)

    def search(self, db: sqlite3.Connection, query: str, limit: int) -> tuple[SearchStatus, list[SearchResult]]:
        terms = dict.fromkeys(query.split())
        if not terms:
            return "usage", []
        if self._tokenizer is None:
            return "unavailable", []
        status: SearchStatus = "ok" if self._tokenizer == "trigram" else "degraded"
        # FTS5 terminates the MATCH expression at a NUL byte.
        if any(len(term) >= 3 and "\0" in term for term in terms):
            return status, []

        db.execute("BEGIN")
        if self._active_tokenizer(db) != self._tokenizer:
            # Another process may have switched capabilities since this store opened.
            db.rollback()
            db.execute("BEGIN IMMEDIATE")
            self.synchronize(db)

        table = _TABLES[self._tokenizer]
        clauses = []
        params = []
        phrases = ['"' + term.replace('"', '""') + '"' for term in terms if len(term) >= 3]
        if phrases:
            clauses.append(f"{table} MATCH ?")
            params.append(" AND ".join(phrases))
        short_terms = [term for term in terms if len(term) < 3]
        if short_terms:
            values = ", ".join("(?)" for _ in short_terms)
            clauses.append(
                f"NOT EXISTS (SELECT 1 FROM (VALUES {values}) AS terms "
                "WHERE instr(question, terms.column1) = 0 AND instr(body, terms.column1) = 0)"
            )
            params.extend(short_terms)
        params.append(max(0, min(limit, _MAX_RESULTS)))
        rows = db.execute(
            f"""
            SELECT records.id, records.created_utc, records.request
            FROM {table} JOIN records ON records.id = {table}.rowid
            WHERE {" AND ".join(clauses)}
            ORDER BY records.created_utc DESC, records.id DESC LIMIT ?
            """,
            params,
        )
        return status, [SearchResult(id, timestamp[:10], _preview(question)) for id, timestamp, question in rows]


def _preview(question: str) -> str:
    line = " ".join(question.split())
    return line if len(line) <= _PREVIEW_LENGTH else line[: _PREVIEW_LENGTH - 1] + "…"
