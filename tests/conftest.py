"""Temporary archive fixtures; all example cards are synthetic."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from contacts.app import create_app
from contacts.archive import ContactArchive
from contacts.storage import SQLiteArchiveStore


@pytest.fixture
def store(tmp_path: Path) -> SQLiteArchiveStore:
    return SQLiteArchiveStore(tmp_path / "archive.sqlite3")


@pytest.fixture
def archive(store: SQLiteArchiveStore) -> ContactArchive:
    return ContactArchive(store)


@pytest.fixture
def app(store: SQLiteArchiveStore) -> Iterator[Flask]:
    application = create_app(
        {"TESTING": True, "SECRET_KEY": "test-only-secret", "APP_ENV": "development"},
        store=store,
    )
    yield application


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def csrf(client: FlaskClient) -> str:
    with client.session_transaction() as session:
        return str(session["csrf_token"])
