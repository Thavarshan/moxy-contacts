"""Flask page, security, and archive flow tests use synthetic contact data only."""

from io import BytesIO
from pathlib import Path

from flask import Flask
from flask.testing import FlaskClient

from contacts.app import create_app
from contacts.archive import ContactArchive
from contacts.storage import SQLiteArchiveStore

FIXTURE = Path(__file__).parent / "fixtures" / "iphone_style.vcf"
CARD = FIXTURE.read_bytes()


def csrf(client: FlaskClient) -> str:
    client.get("/")
    with client.session_transaction() as session:
        return str(session["csrf_token"])


def test_home_is_discreet_even_when_archive_contains_contacts(
    client: FlaskClient, archive: ContactArchive
) -> None:
    archive.import_vcards(CARD)

    response = client.get("/")

    assert response.status_code == 200
    assert b"Import a vCard" in response.data
    assert b"Search the archive" in response.data
    assert b"Browse the archive" in response.data
    assert b"Dr. Zo" not in response.data
    assert b"zoe.work@example.test" not in response.data


def test_browse_only_shows_contacts_after_explicit_visit(
    client: FlaskClient, archive: ContactArchive
) -> None:
    archive.import_vcards(CARD)

    response = client.get("/contacts")

    assert response.status_code == 200
    assert b"Dr. Zo\xc3\xab S. Koda" in response.data
    assert b"Browse contacts" in response.data


def test_search_results_are_deliberate_and_use_casefolded_text(
    client: FlaskClient, archive: ContactArchive
) -> None:
    archive.import_vcards(CARD)

    response = client.get("/search", query_string={"q": "ZOË"})

    assert response.status_code == 200
    assert b"Dr. Zo\xc3\xab S. Koda" in response.data
    assert b"zoe.work@example.test" not in client.get("/").data


def test_import_reports_good_entries_and_malformed_entries(client: FlaskClient) -> None:
    bad = b"BEGIN:VCARD\nVERSION:3.0\nFN:Broken contact\n"
    response = client.post(
        "/contacts/import",
        data={
            "csrf_token": csrf(client),
            "file": (BytesIO(CARD), "selected-contacts.vcf"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert b"contact imported" in response.data
    assert b"original vCards are retained exactly" in response.data

    response = client.post(
        "/contacts/import",
        data={
            "csrf_token": csrf(client),
            "file": (BytesIO(CARD), "mixed.vcf"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert b"exact duplicate" in response.data

    response = client.post(
        "/contacts/import",
        data={
            "csrf_token": csrf(client),
            "file": (BytesIO(CARD + bad), "mixed-with-error.vcf"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert b"incomplete" in response.data
    assert b"Broken contact" not in response.data


def test_invalid_upload_is_rejected_without_echoing_its_content(client: FlaskClient) -> None:
    secretish = b"BEGIN:VCARD\nNOTE:private random note\nEND:VCARD\n"
    response = client.post(
        "/contacts/import",
        data={
            "csrf_token": csrf(client),
            "file": (BytesIO(secretish), "not-a-contact.vcf"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert b"formatted name" in response.data
    assert b"private random note" not in response.data


def test_create_view_edit_export_and_reimport_round_trip(
    client: FlaskClient, archive: ContactArchive, tmp_path: Path
) -> None:
    create = client.post(
        "/contacts/new",
        data={"csrf_token": csrf(client), "vcard": CARD.decode("utf-8")},
    )
    assert create.status_code == 302
    contact_id = archive.list_contacts()[0].id

    edit_page = client.get(f"/contacts/{contact_id}/edit")
    assert edit_page.status_code == 200
    edited = CARD.replace(b"Keep this unknown value", b"Updated custom data")
    update = client.post(
        f"/contacts/{contact_id}/edit",
        data={"csrf_token": csrf(client), "vcard": edited.decode("utf-8")},
    )
    assert update.status_code == 302

    exported = client.get(f"/contacts/{contact_id}/export.vcf")
    assert exported.status_code == 200
    assert exported.data == edited
    assert "private" in exported.headers["Cache-Control"]
    assert "no-store" in exported.headers["Cache-Control"]

    other_store = SQLiteArchiveStore(tmp_path / "round-trip.sqlite3")
    other_archive = ContactArchive(other_store)
    restored = other_archive.import_vcards(exported.data)
    assert restored.imported_count == 1
    returned = other_archive.export_contact(restored.contact_ids[0])
    assert returned == edited


def test_contact_detail_shows_unknown_data_safely_and_original_is_available(
    client: FlaskClient, archive: ContactArchive
) -> None:
    contact_id = archive.import_vcards(CARD).contact_ids[0]

    detail = client.get(f"/contacts/{contact_id}")
    original = client.get(f"/contacts/{contact_id}/original.vcf")

    assert detail.status_code == 200
    assert b"X-CUSTOM-PROPERTY" in detail.data
    assert b"Keep this unknown value" in detail.data
    assert original.data == CARD
    assert "private" in original.headers["Cache-Control"]


def test_embedded_photo_is_served_only_through_private_contact_route(
    client: FlaskClient, archive: ContactArchive
) -> None:
    contact_id = archive.import_vcards(CARD).contact_ids[0]

    response = client.get(f"/contacts/{contact_id}/photo")

    assert response.status_code == 200
    assert response.content_type == "image/jpeg"
    assert response.data.startswith(b"\xff\xd8\xff")
    assert "no-store" in response.headers["Cache-Control"]


def test_confirmed_delete_removes_record_from_browse_search_and_export(
    client: FlaskClient, archive: ContactArchive
) -> None:
    contact_id = archive.import_vcards(CARD).contact_ids[0]
    confirm = client.get(f"/contacts/{contact_id}/delete")
    deleted = client.post(
        f"/contacts/{contact_id}/delete",
        data={"csrf_token": csrf(client), "confirm": "yes"},
    )

    assert confirm.status_code == 200
    assert b"does not remove anything from your iPhone or iCloud" in confirm.data
    assert deleted.status_code == 302
    assert client.get(f"/contacts/{contact_id}/export.vcf").status_code == 404
    assert client.get("/search?q=zoe").status_code == 200
    assert b"Dr. Zo" not in client.get("/contacts").data


def test_backup_is_downloaded_as_private_zip_and_restore_skips_duplicates(
    client: FlaskClient, archive: ContactArchive
) -> None:
    archive.import_vcards(CARD)
    backup = client.get("/archive/backup.zip")
    assert backup.status_code == 200
    assert backup.data.startswith(b"PK\x03\x04")
    assert "attachment" in backup.headers["Content-Disposition"]
    assert "no-store" in backup.headers["Cache-Control"]

    restored = client.post(
        "/archive/restore",
        data={
            "csrf_token": csrf(client),
            "file": (BytesIO(backup.data), "moxy-backup.zip"),
        },
        content_type="multipart/form-data",
    )
    assert restored.status_code == 200
    assert b"exact duplicate" in restored.data
    assert len(archive.list_contacts()) == 1


def test_all_mutations_require_csrf_and_methods_keep_http_error_statuses(
    client: FlaskClient,
) -> None:
    missing_csrf = client.post(
        "/contacts/import",
        data={"file": (BytesIO(CARD), "selected.vcf")},
        content_type="multipart/form-data",
    )
    unsupported = client.put("/contacts/import")
    not_found = client.get("/no-such-page")

    assert missing_csrf.status_code == 400
    assert unsupported.status_code == 405
    assert not_found.status_code == 404
    assert b"405" in unsupported.data


def test_public_assets_have_public_cache_but_contact_data_never_does(
    client: FlaskClient, archive: ContactArchive
) -> None:
    contact_id = archive.import_vcards(CARD).contact_ids[0]

    asset = client.get("/css/style.css")
    contact = client.get(f"/contacts/{contact_id}/export.vcf")

    assert asset.status_code == 200
    assert "public" in asset.headers["Cache-Control"]
    assert "private" in contact.headers["Cache-Control"]
    assert "no-store" in contact.headers["Cache-Control"]


def test_production_without_a_d1_binding_fails_closed_even_with_fake_access_headers(
    tmp_path: Path,
) -> None:
    app = create_app(
        {"APP_ENV": "production", "TESTING": True, "SECRET_KEY": "test-secret"},
        local_database_path=tmp_path / "not-used.sqlite3",
    )
    response = app.test_client().get(
        "/",
        headers={"Cf-Access-User-Email": "owner@example.test", "Cf-Access-Jwt-Assertion": "fake"},
        environ_overrides={"workers.env": {"APP_ENV": "production", "SESSION_SECRET": "secret"}},
    )

    assert response.status_code == 503
    assert b"not configured yet" in response.data


def test_production_without_session_secret_stays_closed_with_a_database(
    store: SQLiteArchiveStore,
) -> None:
    app = create_app(
        {"APP_ENV": "production", "TESTING": True},
        store=store,
    )
    response = app.test_client().get(
        "/",
        headers={"Cf-Access-User-Email": "owner@example.test"},
        environ_overrides={"workers.env": {"APP_ENV": "production", "DB": object()}},
    )

    assert response.status_code == 503
    assert b"private archive is not ready" in response.data.lower()


def test_production_cookie_is_secure_and_no_content_is_logged(
    store: SQLiteArchiveStore, caplog
) -> None:
    app: Flask = create_app(
        {"APP_ENV": "production", "TESTING": True, "SECRET_KEY": "test-secret"},
        store=store,
    )
    client = app.test_client()
    home_response = client.get("/")
    contact_id = ContactArchive(store).import_vcards(CARD).contact_ids[0]
    response = client.get(f"/contacts/{contact_id}")

    assert home_response.status_code == 200
    assert "Secure" in home_response.headers["Set-Cookie"]
    assert "HttpOnly" in home_response.headers["Set-Cookie"]
    assert "SameSite=Lax" in home_response.headers["Set-Cookie"]
    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    assert "Dr. Zoë S. Koda" not in caplog.text
