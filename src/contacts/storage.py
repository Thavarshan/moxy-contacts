"""Local SQLite and Cloudflare D1 storage for private vCard records."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations" / "0001_initial.sql"


class ArchiveStoreError(RuntimeError):
    """Storage failed without including record contents in the error message."""


@dataclass(frozen=True, slots=True)
class ContactSummary:
    id: str
    display_name: str
    search_text: str
    duplicate_key: str
    original_sha256: str
    created_at: str
    updated_at: str
    original_size: int
    current_size: int


@dataclass(frozen=True, slots=True)
class ContactRecord(ContactSummary):
    original_vcard: bytes
    current_vcard: bytes


class ArchiveStore(Protocol):
    """Storage operations used by the small contact archive service."""

    def list_summaries(self) -> list[ContactSummary]: ...

    def get_contact(self, contact_id: str) -> ContactRecord | None: ...

    def all_contacts(self) -> list[ContactRecord]: ...

    def insert_contact(self, contact: ContactRecord) -> None: ...

    def update_contact(self, contact: ContactRecord) -> bool: ...

    def delete_contact(self, contact_id: str) -> bool: ...


class SQLiteArchiveStore:
    """Straightforward, file-backed local storage for learning and offline tests."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(MIGRATION.read_text(encoding="utf-8"))

    def list_summaries(self) -> list[ContactSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM contacts ORDER BY display_name COLLATE NOCASE, id"
            ).fetchall()
        return [_summary_from_mapping(row) for row in rows]

    def get_contact(self, contact_id: str) -> ContactRecord | None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT contacts.*, contact_documents.version, contact_documents.vcard
                FROM contacts LEFT JOIN contact_documents
                  ON contacts.id = contact_documents.contact_id
                WHERE contacts.id = ?
                ORDER BY contact_documents.version
                """,
                (contact_id,),
            ).fetchall()
        return _record_from_rows([dict(row) for row in rows])

    def all_contacts(self) -> list[ContactRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT contacts.*, contact_documents.version, contact_documents.vcard
                FROM contacts JOIN contact_documents
                  ON contacts.id = contact_documents.contact_id
                ORDER BY contacts.display_name COLLATE NOCASE, contacts.id
                """
            ).fetchall()
        return _records_from_rows([dict(row) for row in rows])

    def insert_contact(self, contact: ContactRecord) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(_INSERT_CONTACT, _summary_values(contact))
            connection.executemany(
                "INSERT INTO contact_documents (contact_id, version, vcard) VALUES (?, ?, ?)",
                [
                    (contact.id, "original", contact.original_vcard.decode("utf-8")),
                    (contact.id, "current", contact.current_vcard.decode("utf-8")),
                ],
            )

    def update_contact(self, contact: ContactRecord) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE contacts SET display_name=?, search_text=?, duplicate_key=?,
                    updated_at=?, current_size=? WHERE id=?
                """,
                (
                    contact.display_name,
                    contact.search_text,
                    contact.duplicate_key,
                    contact.updated_at,
                    contact.current_size,
                    contact.id,
                ),
            )
            if cursor.rowcount == 0:
                return False
            connection.execute(
                "UPDATE contact_documents SET vcard=? WHERE contact_id=? AND version='current'",
                (contact.current_vcard.decode("utf-8"), contact.id),
            )
        return True

    def delete_contact(self, contact_id: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM contact_documents WHERE contact_id=?", (contact_id,))
            cursor = connection.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
        return cursor.rowcount > 0

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


class D1ArchiveStore:
    """Synchronous Flask adapter for Cloudflare's Workers D1 binding."""

    def __init__(self, database: Any) -> None:
        self._database = database

    def list_summaries(self) -> list[ContactSummary]:
        rows = self._all("SELECT * FROM contacts ORDER BY display_name COLLATE NOCASE, id")
        return [_summary_from_mapping(row) for row in rows]

    def get_contact(self, contact_id: str) -> ContactRecord | None:
        rows = self._all(
            """
            SELECT contacts.*, contact_documents.version, contact_documents.vcard
            FROM contacts LEFT JOIN contact_documents
              ON contacts.id = contact_documents.contact_id
            WHERE contacts.id = ? ORDER BY contact_documents.version
            """,
            (contact_id,),
        )
        return _record_from_rows(rows)

    def all_contacts(self) -> list[ContactRecord]:
        rows = self._all(
            """
            SELECT contacts.*, contact_documents.version, contact_documents.vcard
            FROM contacts JOIN contact_documents
              ON contacts.id = contact_documents.contact_id
            ORDER BY contacts.display_name COLLATE NOCASE, contacts.id
            """
        )
        return _records_from_rows(rows)

    def insert_contact(self, contact: ContactRecord) -> None:
        statements = [
            self._statement(_INSERT_CONTACT, _summary_values(contact)),
            self._statement(
                "INSERT INTO contact_documents (contact_id, version, vcard) VALUES (?, ?, ?)",
                (contact.id, "original", contact.original_vcard.decode("utf-8")),
            ),
            self._statement(
                "INSERT INTO contact_documents (contact_id, version, vcard) VALUES (?, ?, ?)",
                (contact.id, "current", contact.current_vcard.decode("utf-8")),
            ),
        ]
        self._batch(statements)

    def update_contact(self, contact: ContactRecord) -> bool:
        statements = [
            self._statement(
                """
                UPDATE contacts SET display_name=?, search_text=?, duplicate_key=?,
                    updated_at=?, current_size=? WHERE id=?
                """,
                (
                    contact.display_name,
                    contact.search_text,
                    contact.duplicate_key,
                    contact.updated_at,
                    contact.current_size,
                    contact.id,
                ),
            ),
            self._statement(
                "UPDATE contact_documents SET vcard=? WHERE contact_id=? AND version='current'",
                (contact.current_vcard.decode("utf-8"), contact.id),
            ),
        ]
        self._batch(statements)
        return self.get_contact(contact.id) is not None

    def delete_contact(self, contact_id: str) -> bool:
        before = self._one("SELECT id FROM contacts WHERE id=?", (contact_id,))
        if before is None:
            return False
        self._batch(
            [
                self._statement("DELETE FROM contact_documents WHERE contact_id=?", (contact_id,)),
                self._statement("DELETE FROM contacts WHERE id=?", (contact_id,)),
            ]
        )
        return True

    def _all(self, sql: str, parameters: Sequence[object] = ()) -> list[Mapping[str, Any]]:
        statement = self._statement(sql, parameters)
        result = _run_sync(statement.all())
        raw_rows = result["results"] if isinstance(result, Mapping) else result.results
        return [_mapping_from_js(row) for row in raw_rows]

    def _one(self, sql: str, parameters: Sequence[object] = ()) -> Mapping[str, Any] | None:
        rows = self._all(sql, parameters)
        return rows[0] if rows else None

    def _statement(self, sql: str, parameters: Sequence[object] = ()) -> Any:
        statement = self._database.prepare(sql)
        return statement.bind(*parameters) if parameters else statement

    def _batch(self, statements: list[Any]) -> Any:
        return _run_sync(self._database.batch(statements))


def _run_sync(awaitable: Any) -> Any:
    try:
        from pyodide.ffi import run_sync
    except ImportError as exc:
        raise RuntimeError("D1 storage is only available inside a Python Worker.") from exc
    return run_sync(awaitable)


def _mapping_from_js(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    try:
        import pyodide.ffi

        converter_name = "to_py"
        convert_to_python = getattr(pyodide.ffi, converter_name)
        converted = convert_to_python(value, dict_converter=dict)
    except (ImportError, TypeError, ValueError) as exc:
        raise ArchiveStoreError("Cloudflare D1 returned an unreadable query result.") from exc
    if not isinstance(converted, Mapping):
        raise ArchiveStoreError("Cloudflare D1 returned an unreadable query result.")
    return converted


def _summary_values(contact: ContactSummary) -> tuple[object, ...]:
    return (
        contact.id,
        contact.display_name,
        contact.search_text,
        contact.duplicate_key,
        contact.original_sha256,
        contact.created_at,
        contact.updated_at,
        contact.original_size,
        contact.current_size,
    )


_INSERT_CONTACT = """
INSERT INTO contacts (
    id, display_name, search_text, duplicate_key, original_sha256,
    created_at, updated_at, original_size, current_size
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _summary_from_mapping(row: Mapping[str, Any]) -> ContactSummary:
    return ContactSummary(
        id=str(row["id"]),
        display_name=str(row["display_name"]),
        search_text=str(row["search_text"]),
        duplicate_key=str(row["duplicate_key"]),
        original_sha256=str(row["original_sha256"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        original_size=int(row["original_size"]),
        current_size=int(row["current_size"]),
    )


def _record_from_rows(rows: Sequence[Mapping[str, Any]]) -> ContactRecord | None:
    if not rows:
        return None
    documents: dict[str, bytes] = {}
    for row in rows:
        version = row.get("version")
        value = row.get("vcard")
        if version and value is not None:
            documents[str(version)] = _as_utf8_bytes(value)
    if "original" not in documents or "current" not in documents:
        raise ArchiveStoreError("A contact record is missing a private vCard document.")
    return ContactRecord(
        **_summary_values_as_dict(_summary_from_mapping(rows[0])),
        original_vcard=documents["original"],
        current_vcard=documents["current"],
    )


def _records_from_rows(rows: Sequence[Mapping[str, Any]]) -> list[ContactRecord]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row["id"]), []).append(row)
    records = [_record_from_rows(group) for group in groups.values()]
    return [record for record in records if record is not None]


def _summary_values_as_dict(summary: ContactSummary) -> dict[str, Any]:
    return {
        "id": summary.id,
        "display_name": summary.display_name,
        "search_text": summary.search_text,
        "duplicate_key": summary.duplicate_key,
        "original_sha256": summary.original_sha256,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
        "original_size": summary.original_size,
        "current_size": summary.current_size,
    }


def _as_utf8_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")
