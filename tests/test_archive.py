"""Archive behavior tests use only synthetic vCards and a temporary SQLite database."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from contacts.archive import ArchiveError, ContactArchive
from contacts.storage import SQLiteArchiveStore
from contacts.vcards import parse_vcard

FIXTURE = Path(__file__).parent / "fixtures" / "iphone_style.vcf"
SOURCE_CARD = FIXTURE.read_bytes()


@pytest.fixture
def store(tmp_path: Path) -> SQLiteArchiveStore:
    return SQLiteArchiveStore(tmp_path / "archive.sqlite3")


def test_import_persists_and_can_be_read_from_a_new_store_instance(
    store: SQLiteArchiveStore,
) -> None:
    report = ContactArchive(store).import_vcards(SOURCE_CARD)
    reopened = ContactArchive(SQLiteArchiveStore(store.path))

    assert report.imported_count == 1
    assert reopened.get_contact(report.contact_ids[0]).original_vcard == SOURCE_CARD
    assert reopened.get_contact(report.contact_ids[0]).current_vcard == SOURCE_CARD


def test_exact_duplicate_is_reported_and_skipped_unless_user_opts_in(
    store: SQLiteArchiveStore,
) -> None:
    archive = ContactArchive(store)
    first = archive.import_vcards(SOURCE_CARD)
    skipped = archive.import_vcards(SOURCE_CARD)
    repeated = archive.import_vcards(SOURCE_CARD, include_exact_duplicates=True)

    assert first.imported_count == repeated.imported_count == 1
    assert skipped.imported_count == 0
    assert skipped.exact_duplicates == 1
    assert len(archive.list_contacts()) == 2
    assert first.contact_ids[0] != repeated.contact_ids[0]


def test_possible_duplicate_is_kept_as_a_separate_contact_with_a_warning(
    store: SQLiteArchiveStore,
) -> None:
    archive = ContactArchive(store)
    archive.import_vcards(SOURCE_CARD)
    slightly_changed = SOURCE_CARD.replace(b"Met at a synthetic", b"Met at another synthetic")

    report = archive.import_vcards(slightly_changed)

    assert report.imported_count == 1
    assert report.possible_duplicates == 1
    assert len(archive.list_contacts()) == 2


def test_edit_changes_export_but_keeps_exact_original_for_backup(
    store: SQLiteArchiveStore,
) -> None:
    archive = ContactArchive(store)
    contact_id = archive.import_vcards(SOURCE_CARD).contact_ids[0]
    edited = SOURCE_CARD.replace(b"Dr. Zo\xc3\xab S. Koda", b"Professor Zo\xc3\xab S. Koda")
    edited = edited.replace(b"zoe.work@example.test", b"zoe.new@example.test")

    contact = archive.update_contact(contact_id, edited)

    assert contact.original_vcard == SOURCE_CARD
    assert contact.current_vcard == edited
    assert parse_vcard(archive.export_contact(contact_id)).display_name == "Professor Zoë S. Koda"


def test_search_matches_unicode_and_repeated_contact_properties(store: SQLiteArchiveStore) -> None:
    archive = ContactArchive(store)
    archive.import_vcards(SOURCE_CARD)

    assert [c.display_name for c in archive.search_contacts("ZOË")] == ["Dr. Zoë S. Koda"]
    assert [c.display_name for c in archive.search_contacts("zoe.work@example.test")]
    assert [c.display_name for c in archive.search_contacts("Colombo")]
    assert archive.search_contacts("not present") == []


def test_create_contact_validates_one_card_and_assigns_a_new_id(store: SQLiteArchiveStore) -> None:
    archive = ContactArchive(store)

    contact = archive.create_contact(SOURCE_CARD)

    assert contact.id
    assert contact.original_vcard == SOURCE_CARD
    assert len(archive.list_contacts()) == 1
    with pytest.raises(ArchiveError, match="one contact"):
        archive.create_contact(SOURCE_CARD + SOURCE_CARD)


def test_large_vcard_metadata_is_bounded_before_storage(store: SQLiteArchiveStore) -> None:
    archive = ContactArchive(store)
    long_name = "Zoë " * 30_000
    payload = (
        "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:"
        + long_name
        + "\r\nTEL;TYPE=CELL:+1-555-0100\r\nEND:VCARD\r\n"
    ).encode("utf-8")

    contact = archive.create_contact(payload)

    assert contact.original_vcard == payload
    assert len(contact.display_name.encode("utf-8")) <= 4096
    assert len(contact.search_text.encode("utf-8")) <= 128 * 1024
    assert len(contact.duplicate_key) == 64


def test_backup_contains_original_current_and_sha256_manifest(store: SQLiteArchiveStore) -> None:
    archive = ContactArchive(store)
    contact_id = archive.import_vcards(SOURCE_CARD).contact_ids[0]
    edited = SOURCE_CARD.replace(b"Keep this unknown value", b"Keep this edited value")
    archive.update_contact(contact_id, edited)

    backup = archive.create_backup()

    with zipfile.ZipFile(io.BytesIO(backup)) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        row = manifest["contacts"][0]
        original = bundle.read(row["original_path"])
        current = bundle.read(row["current_path"])
    assert original == SOURCE_CARD
    assert current == edited
    assert row["original_sha256"] == hashlib.sha256(original).hexdigest()
    assert row["current_sha256"] == hashlib.sha256(current).hexdigest()


def test_restore_recovers_both_versions_and_never_overwrites_existing_data(
    store: SQLiteArchiveStore, tmp_path: Path
) -> None:
    source = ContactArchive(store)
    original_id = source.import_vcards(SOURCE_CARD).contact_ids[0]
    edited = SOURCE_CARD.replace(b"Keep this unknown value", b"Current edited value")
    source.update_contact(original_id, edited)
    backup = source.create_backup()

    target = ContactArchive(SQLiteArchiveStore(tmp_path / "recovered.sqlite3"))
    restored = target.restore_backup(backup)
    repeated = target.restore_backup(backup)

    assert restored.imported_count == 1
    assert repeated.imported_count == 0
    assert repeated.exact_duplicates == 1
    recovered = target.get_contact(original_id)
    assert recovered.original_vcard == SOURCE_CARD
    assert recovered.current_vcard == edited


def test_restore_checks_all_hashes_before_writing_any_contact(
    store: SQLiteArchiveStore, tmp_path: Path
) -> None:
    archive = ContactArchive(store)
    archive.import_vcards(SOURCE_CARD)
    backup = archive.create_backup()
    with zipfile.ZipFile(io.BytesIO(backup)) as bundle:
        members = {name: bundle.read(name) for name in bundle.namelist()}
    manifest = json.loads(members["manifest.json"])
    row = manifest["contacts"][0]
    members[row["current_path"]] = SOURCE_CARD.replace("Zoë".encode(), b"Other")
    changed = io.BytesIO()
    with zipfile.ZipFile(changed, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, data in members.items():
            bundle.writestr(name, data)

    empty = ContactArchive(SQLiteArchiveStore(tmp_path / "empty.sqlite3"))
    with pytest.raises(ArchiveError, match="checksum"):
        empty.restore_backup(changed.getvalue())
    assert empty.list_contacts() == []


def test_delete_removes_contact_and_both_vcard_versions(store: SQLiteArchiveStore) -> None:
    archive = ContactArchive(store)
    contact_id = archive.import_vcards(SOURCE_CARD).contact_ids[0]

    assert archive.delete_contact(contact_id) is True
    assert archive.get_contact(contact_id) is None
    assert archive.delete_contact(contact_id) is False
