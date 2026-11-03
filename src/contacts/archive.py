"""Contact archive operations, duplicate review, and verified backup bundles."""

from __future__ import annotations

import io
import json
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from contacts.storage import ArchiveStore, ContactRecord, ContactSummary
from contacts.vcards import (
    MAX_CARD_BYTES,
    ParsedVCard,
    VCardIssue,
    parse_upload,
    parse_vcard,
    sha256,
)

MAX_BACKUP_DATA_BYTES = 28 * 1024 * 1024
MAX_BACKUP_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_BACKUP_CONTACTS = 1000
MAX_DISPLAY_NAME_BYTES = 4096


class ArchiveError(ValueError):
    """An expected, user-safe problem with an archive action."""


@dataclass(frozen=True, slots=True)
class ImportReport:
    contact_ids: tuple[str, ...]
    imported_count: int
    exact_duplicates: int
    possible_duplicates: int
    issues: tuple[VCardIssue, ...]
    id_conflicts: int = 0


class ContactArchive:
    """A small archive service that stores imported bytes and an editable copy."""

    def __init__(self, store: ArchiveStore) -> None:
        self.store = store

    def list_contacts(self) -> list[ContactSummary]:
        return self.store.list_summaries()

    def get_contact(self, contact_id: str) -> ContactRecord | None:
        return self.store.get_contact(contact_id)

    def search_contacts(self, query: str) -> list[ContactSummary]:
        needle = _normalize_search(query)
        if not needle:
            return []
        return [contact for contact in self.store.list_summaries() if needle in contact.search_text]

    def create_contact(self, payload: bytes) -> ContactRecord:
        parsed = _parse_one(payload)
        now = _now()
        contact = _new_contact(parsed, str(uuid.uuid4()), now, now)
        self.store.insert_contact(contact)
        return contact

    def import_vcards(self, payload: bytes, include_exact_duplicates: bool = False) -> ImportReport:
        result = parse_upload(payload)
        known = self.store.list_summaries()
        contact_ids: list[str] = []
        exact_duplicates = 0
        possible_duplicates = 0

        for parsed in result.cards:
            if any(contact.original_sha256 == sha256(parsed.original) for contact in known):
                exact_duplicates += 1
                if not include_exact_duplicates:
                    continue
            if parsed.duplicate_key and any(
                contact.duplicate_key == parsed.duplicate_key for contact in known
            ):
                possible_duplicates += 1
            now = _now()
            contact = _new_contact(parsed, str(uuid.uuid4()), now, now)
            self.store.insert_contact(contact)
            contact_ids.append(contact.id)
            known.append(_as_summary(contact))

        return ImportReport(
            contact_ids=tuple(contact_ids),
            imported_count=len(contact_ids),
            exact_duplicates=exact_duplicates,
            possible_duplicates=possible_duplicates,
            issues=result.issues,
        )

    def update_contact(self, contact_id: str, payload: bytes) -> ContactRecord:
        parsed = _parse_one(payload)
        existing = self.store.get_contact(contact_id)
        if existing is None:
            raise ArchiveError("That contact is no longer in the archive.")
        updated = ContactRecord(
            id=existing.id,
            display_name=_limit_utf8_text(parsed.display_name, MAX_DISPLAY_NAME_BYTES),
            search_text=_limit_search_text(parsed.search_text),
            duplicate_key=parsed.duplicate_key,
            original_sha256=existing.original_sha256,
            created_at=existing.created_at,
            updated_at=_now(),
            original_size=existing.original_size,
            current_size=len(parsed.export_bytes),
            original_vcard=existing.original_vcard,
            current_vcard=parsed.export_bytes,
        )
        if not self.store.update_contact(updated):
            raise ArchiveError("That contact is no longer in the archive.")
        return updated

    def export_contact(self, contact_id: str) -> bytes:
        contact = self.store.get_contact(contact_id)
        if contact is None:
            raise ArchiveError("That contact is no longer in the archive.")
        return contact.current_vcard

    def delete_contact(self, contact_id: str) -> bool:
        return self.store.delete_contact(contact_id)

    def create_backup(self) -> bytes:
        summaries = self.store.list_summaries()
        if len(summaries) > MAX_BACKUP_CONTACTS:
            raise ArchiveError(f"Backups are limited to {MAX_BACKUP_CONTACTS} contacts at a time.")
        estimated_size = sum(item.original_size + item.current_size for item in summaries)
        if estimated_size > MAX_BACKUP_DATA_BYTES:
            raise ArchiveError(
                "This archive is too large for one download. Export smaller groups of contacts."
            )
        contacts = self.store.all_contacts()
        records: list[dict[str, str]] = []
        output = io.BytesIO()
        with zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as bundle:
            for contact in contacts:
                original_path = f"contacts/{contact.id}/original.vcf"
                current_path = f"contacts/{contact.id}/current.vcf"
                bundle.writestr(original_path, contact.original_vcard)
                bundle.writestr(current_path, contact.current_vcard)
                records.append(
                    {
                        "id": contact.id,
                        "original_path": original_path,
                        "current_path": current_path,
                        "original_sha256": sha256(contact.original_vcard),
                        "current_sha256": sha256(contact.current_vcard),
                    }
                )
            manifest = {
                "format": "moxy-contacts-backup",
                "version": 1,
                "created_at": _now(),
                "contacts": records,
            }
            bundle.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        result = output.getvalue()
        if len(result) > MAX_BACKUP_UPLOAD_BYTES:
            raise ArchiveError("The backup is too large to download in one file.")
        return result

    def restore_backup(
        self, payload: bytes, include_exact_duplicates: bool = False
    ) -> ImportReport:
        restored = _read_backup(payload)
        known = self.store.list_summaries()
        contact_ids: list[str] = []
        exact_duplicates = 0
        possible_duplicates = 0
        id_conflicts = 0

        for old_id, original, current in restored:
            parse_vcard(original)
            current_card = parse_vcard(current)
            original_hash = sha256(original)
            if any(contact.original_sha256 == original_hash for contact in known):
                exact_duplicates += 1
                if not include_exact_duplicates:
                    continue
            if current_card.duplicate_key and any(
                contact.duplicate_key == current_card.duplicate_key for contact in known
            ):
                possible_duplicates += 1
            contact_id = old_id
            if any(contact.id == contact_id for contact in known):
                contact_id = str(uuid.uuid4())
                id_conflicts += 1
            now = _now()
            contact = _new_contact(
                current_card,
                contact_id,
                now,
                now,
                original=original,
                original_sha256=original_hash,
                original_size=len(original),
            )
            # The original card is immutable; restore the edited export bytes as current.
            contact = replace(contact, original_vcard=original, current_vcard=current)
            self.store.insert_contact(contact)
            known.append(_as_summary(contact))
            contact_ids.append(contact.id)

        return ImportReport(
            contact_ids=tuple(contact_ids),
            imported_count=len(contact_ids),
            exact_duplicates=exact_duplicates,
            possible_duplicates=possible_duplicates,
            issues=(),
            id_conflicts=id_conflicts,
        )


def _read_backup(payload: bytes) -> list[tuple[str, bytes, bytes]]:
    if len(payload) > MAX_BACKUP_UPLOAD_BYTES:
        raise ArchiveError("The backup upload must be 32 MB or smaller.")
    try:
        bundle = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError("Choose a valid Moxy backup ZIP file.") from exc

    with bundle:
        members = bundle.infolist()
        if len(members) > MAX_BACKUP_CONTACTS * 2 + 1:
            raise ArchiveError("The backup contains too many files.")
        if len({item.filename for item in members}) != len(members):
            raise ArchiveError("The backup has duplicate file names.")
        manifest_info = next((item for item in members if item.filename == "manifest.json"), None)
        if manifest_info is None or manifest_info.file_size > 1024 * 1024:
            raise ArchiveError("The backup is missing a valid manifest.")
        try:
            manifest = json.loads(bundle.read("manifest.json"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            raise ArchiveError("The backup manifest could not be read.") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != "moxy-contacts-backup"
            or manifest.get("version") != 1
            or not isinstance(manifest.get("contacts"), list)
        ):
            raise ArchiveError("This is not a supported Moxy backup.")
        rows = manifest["contacts"]
        if len(rows) > MAX_BACKUP_CONTACTS:
            raise ArchiveError(f"Backups are limited to {MAX_BACKUP_CONTACTS} contacts.")

        expected_names = {"manifest.json"}
        restored: list[tuple[str, bytes, bytes]] = []
        total_bytes = 0
        seen_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ArchiveError("A backup contact entry is invalid.")
            try:
                contact_id = str(uuid.UUID(str(row["id"])))
                if contact_id != row["id"] or contact_id in seen_ids:
                    raise ValueError
                seen_ids.add(contact_id)
                original_path = f"contacts/{contact_id}/original.vcf"
                current_path = f"contacts/{contact_id}/current.vcf"
                if row["original_path"] != original_path or row["current_path"] != current_path:
                    raise ValueError
                original_info = bundle.getinfo(original_path)
                current_info = bundle.getinfo(current_path)
            except (KeyError, ValueError, zipfile.BadZipFile) as exc:
                raise ArchiveError(
                    "A backup contact entry has an invalid file path or ID."
                ) from exc
            if original_info.file_size > MAX_CARD_BYTES or current_info.file_size > MAX_CARD_BYTES:
                raise ArchiveError("A backup contains a contact larger than 1.9 MB.")
            original = bundle.read(original_path)
            current = bundle.read(current_path)
            total_bytes += len(original) + len(current)
            if total_bytes > MAX_BACKUP_DATA_BYTES:
                raise ArchiveError("The expanded backup is too large.")
            if row.get("original_sha256") != sha256(original) or row.get(
                "current_sha256"
            ) != sha256(current):
                raise ArchiveError("A backup file failed its SHA-256 checksum check.")
            try:
                parse_vcard(original)
                parse_vcard(current)
            except ValueError as exc:
                raise ArchiveError("A backup contains a malformed vCard.") from exc
            expected_names.update((original_path, current_path))
            restored.append((contact_id, original, current))
        if {item.filename for item in members} != expected_names:
            raise ArchiveError("The backup contains unexpected files.")
        return restored


def _parse_one(payload: bytes) -> ParsedVCard:
    if len(payload) > MAX_CARD_BYTES:
        raise ArchiveError("Each contact must be 1.9 MB or smaller.")
    upload = parse_upload(payload)
    if upload.issues or len(upload.cards) != 1:
        message = upload.issues[0].message if upload.issues else "Upload exactly one contact."
        raise ArchiveError(message)
    return upload.cards[0]


def _new_contact(
    parsed: ParsedVCard,
    contact_id: str,
    created_at: str,
    updated_at: str,
    *,
    original: bytes | None = None,
    original_sha256: str | None = None,
    original_size: int | None = None,
) -> ContactRecord:
    original_bytes = original if original is not None else parsed.original
    return ContactRecord(
        id=contact_id,
        display_name=_limit_utf8_text(parsed.display_name, MAX_DISPLAY_NAME_BYTES),
        search_text=_limit_search_text(parsed.search_text),
        duplicate_key=parsed.duplicate_key,
        original_sha256=original_sha256 or sha256(original_bytes),
        created_at=created_at,
        updated_at=updated_at,
        original_size=original_size if original_size is not None else len(original_bytes),
        current_size=len(parsed.export_bytes),
        original_vcard=original_bytes,
        current_vcard=parsed.export_bytes,
    )


def _as_summary(contact: ContactRecord) -> ContactSummary:
    return ContactSummary(
        id=contact.id,
        display_name=contact.display_name,
        search_text=contact.search_text,
        duplicate_key=contact.duplicate_key,
        original_sha256=contact.original_sha256,
        created_at=contact.created_at,
        updated_at=contact.updated_at,
        original_size=contact.original_size,
        current_size=contact.current_size,
    )


def _limit_search_text(value: str) -> str:
    return _limit_utf8_text(value, 128 * 1024)


def _limit_utf8_text(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")[:limit]
    return encoded.decode("utf-8", errors="ignore")


def _normalize_search(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
