"""Exercise the app, D1 binding, and static assets in the actual Workers runtime."""

from __future__ import annotations

import http.cookiejar
import io
import os
import re
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import suppress
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "iphone_style.vcf"


class FormFields(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden_fields: dict[str, str] = {}
        self.contact_links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "input" and values.get("type") == "hidden":
            self.hidden_fields[str(values.get("name"))] = str(values.get("value", ""))
        if tag == "a" and str(values.get("href", "")).startswith("/contacts/"):
            self.contact_links.append(str(values["href"]))


def free_local_port() -> int:
    with socket.socket() as local_socket:
        local_socket.bind(("127.0.0.1", 0))
        return int(local_socket.getsockname()[1])


def post_form(opener: urllib.request.OpenerDirector, url: str, fields: dict[str, str]) -> bytes:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with opener.open(request, timeout=15) as response:
        assert response.status == 200 or response.status == 302
        return response.read()


def csrf_token(opener: urllib.request.OpenerDirector, url: str) -> str:
    with opener.open(url, timeout=15) as response:
        page = response.read().decode("utf-8")
    fields = FormFields()
    fields.feed(page)
    assert fields.hidden_fields.get("csrf_token")
    return fields.hidden_fields["csrf_token"]


def multipart_upload(token: str, filename: str, payload: bytes) -> tuple[bytes, str]:
    boundary = "moxy-worker-smoke-boundary"
    chunks = [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\n{token}\r\n'
        ).encode(),
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: text/vcard\r\n\r\n'
        ).encode(),
        payload,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return b"".join(chunks), boundary


@pytest.mark.integration
def test_worker_runs_vcard_round_trip_through_local_d1() -> None:
    environment = os.environ.copy()
    environment["WRANGLER_SEND_METRICS"] = "false"
    port = free_local_port()
    base_url = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="moxy-d1-") as state_dir:
        migration = subprocess.run(
            [
                "npx",
                "wrangler",
                "d1",
                "migrations",
                "apply",
                "DB",
                "--local",
                "--persist-to",
                state_dir,
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert migration.returncode == 0, migration.stdout + migration.stderr

        with tempfile.TemporaryFile(mode="w+t") as worker_log:
            process = subprocess.Popen(
                [
                    "uv",
                    "run",
                    "pywrangler",
                    "dev",
                    "--ip",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--persist-to",
                    state_dir,
                    "--show-interactive-dev-session",
                    "false",
                ],
                cwd=ROOT,
                env=environment,
                stdout=worker_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        worker_log.seek(0)
                        pytest.fail(
                            "pywrangler dev exited before becoming ready "
                            f"(exit {process.returncode}):\n{worker_log.read()}"
                        )
                    try:
                        with urllib.request.urlopen(f"{base_url}/healthz", timeout=2) as response:
                            if response.status == 200:
                                assert response.read() == b'{"status":"ok"}\n'
                                break
                    except (urllib.error.URLError, TimeoutError):
                        time.sleep(1)
                else:
                    worker_log.seek(0)
                    pytest.fail(f"Worker did not become ready:\n{worker_log.read()}")

                opener = urllib.request.build_opener(
                    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
                )
                with opener.open(f"{base_url}/", timeout=10) as response:
                    home = response.read()
                assert b"Import a vCard" in home
                with opener.open(f"{base_url}/css/style.css", timeout=10) as response:
                    assert response.status == 200
                    assert b"--ink" in response.read()

                import_url = f"{base_url}/contacts/import"
                token = csrf_token(opener, import_url)
                fixture = FIXTURE.read_bytes()
                body, boundary = multipart_upload(token, "iphone-selected.vcf", fixture)
                request = urllib.request.Request(
                    import_url,
                    data=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    method="POST",
                )
                with opener.open(request, timeout=20) as response:
                    report = response.read()
                assert b"1" in report and b"contact imported" in report

                with opener.open(f"{base_url}/search?q=zoe", timeout=10) as response:
                    search_page = response.read().decode("utf-8")
                links = FormFields()
                links.feed(search_page)
                contact_path = next(
                    path
                    for path in links.contact_links
                    if re.fullmatch(r"/contacts/[0-9a-f-]{36}", path)
                )
                contact_id = contact_path.removeprefix("/contacts/")
                detail_url = f"{base_url}/contacts/{contact_id}"
                with opener.open(detail_url, timeout=10) as response:
                    detail = response.read()
                assert b"X-CUSTOM-PROPERTY" in detail
                assert b"Keep this unknown value" in detail

                edit_url = f"{detail_url}/edit"
                edited = fixture.replace(
                    b"FN:Dr. Zo\xc3\xab S. Koda", b"FN:Dr. Zo\xc3\xab S. Koda Archive"
                )
                token = csrf_token(opener, edit_url)
                post_form(opener, edit_url, {"csrf_token": token, "vcard": edited.decode("utf-8")})
                with opener.open(f"{detail_url}/export.vcf", timeout=15) as response:
                    exported = response.read()
                    assert "no-store" in response.headers.get("Cache-Control", "")
                assert exported == edited

                token = csrf_token(opener, import_url)
                body, boundary = multipart_upload(token, "edited-export.vcf", exported)
                request = urllib.request.Request(
                    import_url,
                    data=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    method="POST",
                )
                with opener.open(request, timeout=20) as response:
                    reimport_report = response.read()
                assert b"1" in reimport_report and b"contact imported" in reimport_report
                with opener.open(f"{base_url}/archive/backup.zip", timeout=20) as response:
                    backup_bytes = response.read()
                with zipfile.ZipFile(io.BytesIO(backup_bytes)) as backup:
                    assert backup.testzip() is None
                    assert backup.namelist()[0].startswith("contacts/")
                    assert b"Keep this unknown value" in b"".join(
                        backup.read(name)
                        for name in backup.namelist()
                        if name.endswith("original.vcf")
                    )
            finally:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
