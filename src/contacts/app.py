"""A small server-rendered Flask app for one private contacts archive."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable, Mapping
from io import BytesIO
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    abort,
    current_app,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.exceptions import HTTPException
from werkzeug.wrappers import Response

from contacts.archive import ArchiveError, ContactArchive
from contacts.contact_form import (
    ContactFormError,
    build_contact_vcard,
    contact_form_values_from_post,
    contact_form_values_from_vcard,
    empty_contact_form_values,
)
from contacts.storage import ArchiveStore, D1ArchiveStore, SQLiteArchiveStore
from contacts.vcards import parse_photo, parse_vcard

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_DIR = ROOT / "public"
LOCAL_SECRET_KEY = "local-development-only-not-for-real-data"
MAX_UPLOAD_BYTES = 12 * 1024 * 1024


def _editor_mode() -> str:
    return "fields" if request.form.get("editor_mode") == "fields" else "vcard"


def _binding_value(bindings: object, name: str) -> object | None:
    if isinstance(bindings, Mapping):
        return bindings.get(name)
    return getattr(bindings, name, None)


def _load_worker_settings(app: Flask) -> None:
    """Make per-deployment settings available before Flask reads its session."""
    original_wsgi = app.wsgi_app

    def worker_settings_wsgi(environ: dict[str, Any], start_response: Callable[..., Any]) -> Any:
        bindings = environ.get("workers.env")
        if bindings is not None:
            app_environment = _binding_value(bindings, "APP_ENV")
            if app_environment is not None:
                app.config["APP_ENV"] = str(app_environment).casefold()
            environment = str(app.config.get("APP_ENV", "development")).casefold()
            app.config["SESSION_COOKIE_SECURE"] = environment == "production"
            secret = _binding_value(bindings, "SESSION_SECRET")
            if environment == "production":
                app.secret_key = str(secret) if secret else None
            elif secret:
                app.secret_key = str(secret)
            elif not app.secret_key:
                app.secret_key = LOCAL_SECRET_KEY
        return original_wsgi(environ, start_response)

    app.wsgi_app = worker_settings_wsgi  # type: ignore[method-assign]


def create_app(
    config: dict[str, object] | None = None,
    *,
    store: ArchiveStore | None = None,
    local_database_path: str | Path | None = None,
) -> Flask:
    """Create the app; local runs use SQLite and deployed requests use the D1 binding."""
    app = Flask(__name__, static_folder=None, template_folder="templates")
    app.config.from_mapping(
        APP_ENV="development",
        SECRET_KEY=None,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        ARCHIVE_STORE=store,
        LOCAL_DATABASE_PATH=str(local_database_path or ROOT / "instance" / "contacts.sqlite3"),
    )
    if config:
        app.config.update(config)
    if app.config.get("SECRET_KEY"):
        app.secret_key = str(app.config["SECRET_KEY"])
    elif str(app.config["APP_ENV"]).casefold() == "development":
        app.secret_key = LOCAL_SECRET_KEY
    app.config["SESSION_COOKIE_SECURE"] = str(app.config["APP_ENV"]).casefold() == "production"
    _load_worker_settings(app)

    @app.before_request
    def prepare_request() -> Response | tuple[str, int] | None:
        if request.routing_exception is not None or request.endpoint == "local_asset":
            return None
        environment = str(current_app.config["APP_ENV"]).casefold()
        worker_environment = request.environ.get("workers.env")
        has_worker_database = _binding_value(worker_environment, "DB") is not None
        has_injected_store = current_app.config.get("ARCHIVE_STORE") is not None
        if environment == "production" and (
            not current_app.secret_key or (not has_worker_database and not has_injected_store)
        ):
            return render_template(
                "error.html",
                title="Private archive is not ready",
                message="The private archive is not configured yet.",
                status=503,
            ), 503

        if request.endpoint == "health":
            return None
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            supplied = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not supplied or not expected or not secrets.compare_digest(supplied, expected):
                return render_template(
                    "error.html",
                    title="Request could not be verified",
                    message="Reload the page and submit the form again.",
                    status=400,
                ), 400
        if not session.get("csrf_token"):
            session["csrf_token"] = secrets.token_urlsafe(32)
        g.csrf_token = session["csrf_token"]
        return None

    @app.context_processor
    def add_template_helpers() -> dict[str, str]:
        return {"csrf_token": str(getattr(g, "csrf_token", ""))}

    @app.after_request
    def set_response_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; img-src 'self'; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'none'",
        )
        if request.endpoint in {"local_asset", "local_script"}:
            response.headers["Cache-Control"] = "public, max-age=3600"
        else:
            response.headers["Cache-Control"] = "private, no-store"
        if str(current_app.config.get("APP_ENV", "")).casefold() == "production":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    @app.errorhandler(HTTPException)
    def safe_http_error(error: HTTPException) -> tuple[str, int]:
        status = error.code or 500
        messages = {
            404: ("Page not found", "That page or contact is not in the archive."),
            405: ("Method not supported", "That action is not available."),
            413: ("Upload too large", "The upload must be 12 MB or smaller."),
        }
        title, message = messages.get(
            status, ("Request could not be completed", "Check the address and try again.")
        )
        return render_template("error.html", title=title, message=message, status=status), status

    @app.errorhandler(Exception)
    def safe_unexpected_error(error: Exception) -> tuple[str, int]:
        request_id = secrets.token_hex(4)
        logging.getLogger("contacts").error(
            "Request failed id=%s method=%s route=%s type=%s",
            request_id,
            request.method,
            request.url_rule.rule if request.url_rule else "unmatched",
            type(error).__name__,
        )
        return (
            render_template(
                "error.html",
                title="Something went wrong",
                message=f"The request failed. Reference: {request_id}.",
                status=500,
            ),
            500,
        )

    def archive() -> ContactArchive:
        existing = current_app.config.get("ARCHIVE_STORE")
        if existing is not None:
            return ContactArchive(existing)
        bindings = request.environ.get("workers.env")
        database = _binding_value(bindings, "DB") if bindings is not None else None
        if database is not None:
            return ContactArchive(D1ArchiveStore(database))
        if str(current_app.config["APP_ENV"]).casefold() == "production":
            abort(503)
        local_store = SQLiteArchiveStore(current_app.config["LOCAL_DATABASE_PATH"])
        current_app.config["ARCHIVE_STORE"] = local_store
        return ContactArchive(local_store)

    @app.get("/")
    def home() -> str:
        return render_template("home.html")

    @app.get("/healthz")
    def health() -> Response:
        return jsonify(status="ok")

    @app.get("/css/<path:filename>")
    def local_asset(filename: str) -> Response:
        return send_from_directory(PUBLIC_DIR / "css", filename)

    @app.get("/js/<path:filename>")
    def local_script(filename: str) -> Response:
        return send_from_directory(PUBLIC_DIR / "js", filename)

    @app.get("/contacts")
    def browse() -> str:
        return render_template("browse.html", contacts=archive().list_contacts())

    @app.get("/search")
    def search() -> str:
        query = request.args.get("q", "")[:200]
        contacts = archive().search_contacts(query)
        return render_template("browse.html", contacts=contacts, query=query, searched=True)

    @app.route("/contacts/import", methods=["GET", "POST"])
    def import_contacts() -> tuple[str, int] | str:
        if request.method == "GET":
            return render_template("import.html")
        upload = request.files.get("file")
        if upload is None or not upload.filename or not upload.filename.casefold().endswith(".vcf"):
            return render_template(
                "import.html", error="Choose a .vcf file exported from your iPhone."
            ), 400
        report = archive().import_vcards(
            upload.read(),
            include_exact_duplicates=request.form.get("include_exact_duplicates") == "yes",
        )
        status = (
            400
            if report.imported_count == 0 and report.issues and not report.exact_duplicates
            else 200
        )
        return render_template("import_result.html", report=report), status

    @app.route("/contacts/new", methods=["GET", "POST"])
    def new_contact() -> str | Response | tuple[str, int]:
        starter = (
            "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Example;New;;;\r\nFN:New Example\r\nEND:VCARD\r\n"
        )
        if request.method == "GET":
            return render_template(
                "edit.html",
                title="Add a contact",
                vcard=starter,
                editor_mode="fields",
                form_values=empty_contact_form_values(),
            )
        editor_mode = _editor_mode()
        try:
            payload = (
                build_contact_vcard(request.form)
                if editor_mode == "fields"
                else request.form.get("vcard", "").encode("utf-8")
            )
            contact = archive().create_contact(payload)
        except (ArchiveError, ContactFormError) as error:
            return render_template(
                "edit.html",
                title="Add a contact",
                vcard=request.form.get("vcard", ""),
                editor_mode=editor_mode,
                form_values=(
                    contact_form_values_from_post(request.form)
                    if editor_mode == "fields"
                    else empty_contact_form_values()
                ),
                error=str(error),
            ), 400
        return redirect(url_for("contact_detail", contact_id=contact.id))

    @app.get("/contacts/<contact_id>")
    def contact_detail(contact_id: str) -> str:
        contact = archive().get_contact(contact_id)
        if contact is None:
            abort(404)
        card = parse_vcard(contact.current_vcard)
        return render_template(
            "contact.html",
            contact=contact,
            properties=card.properties,
            has_photo=card.photo is not None,
        )

    @app.route("/contacts/<contact_id>/edit", methods=["GET", "POST"])
    def edit_contact(contact_id: str) -> str | Response | tuple[str, int]:
        contact = archive().get_contact(contact_id)
        if contact is None:
            abort(404)
        if request.method == "GET":
            return render_template(
                "edit.html",
                title="Edit contact",
                contact=contact,
                vcard=contact.current_vcard.decode("utf-8"),
                editor_mode="fields",
                form_values=contact_form_values_from_vcard(contact.current_vcard),
            )
        editor_mode = _editor_mode()
        text = request.form.get("vcard", "")
        try:
            payload = (
                build_contact_vcard(request.form, existing_vcard=contact.current_vcard)
                if editor_mode == "fields"
                else text.encode("utf-8")
            )
            archive().update_contact(contact_id, payload)
        except (ArchiveError, ContactFormError) as error:
            return render_template(
                "edit.html",
                title="Edit contact",
                contact=contact,
                vcard=text,
                editor_mode=editor_mode,
                form_values=(
                    contact_form_values_from_post(request.form)
                    if editor_mode == "fields"
                    else contact_form_values_from_vcard(contact.current_vcard)
                ),
                error=str(error),
            ), 400
        return redirect(url_for("contact_detail", contact_id=contact_id))

    @app.get("/contacts/<contact_id>/export.vcf")
    def export_contact(contact_id: str) -> Response:
        contact = archive().get_contact(contact_id)
        if contact is None:
            abort(404)
        payload = contact.current_vcard
        return send_file(
            BytesIO(payload),
            mimetype="text/vcard; charset=utf-8",
            as_attachment=True,
            download_name=f"contact-{contact_id}.vcf",
            max_age=0,
            etag=False,
        )

    @app.get("/contacts/<contact_id>/original.vcf")
    def download_original(contact_id: str) -> Response:
        contact = archive().get_contact(contact_id)
        if contact is None:
            abort(404)
        return send_file(
            BytesIO(contact.original_vcard),
            mimetype="text/vcard; charset=utf-8",
            as_attachment=True,
            download_name=f"contact-{contact_id}-original.vcf",
            max_age=0,
            etag=False,
        )

    @app.get("/contacts/<contact_id>/photo")
    def contact_photo(contact_id: str) -> Response:
        contact = archive().get_contact(contact_id)
        if contact is None:
            abort(404)
        image = parse_photo(contact.current_vcard)
        if image is None:
            abort(404)
        photo, media_type = image
        response = make_response(photo)
        response.headers["Content-Type"] = media_type
        response.headers["Content-Disposition"] = "inline"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/contacts/<contact_id>/delete", methods=["GET", "POST"])
    def delete_contact(contact_id: str) -> str | Response | tuple[str, int]:
        contact = archive().get_contact(contact_id)
        if contact is None:
            abort(404)
        if request.method == "GET":
            return render_template("delete.html", contact=contact)
        if request.form.get("confirm") != "yes":
            return render_template(
                "delete.html", contact=contact, error="Confirm the deletion to continue."
            ), 400
        archive().delete_contact(contact_id)
        return redirect(url_for("browse"))

    @app.get("/archive/backup.zip")
    def download_backup() -> Response:
        payload = archive().create_backup()
        return send_file(
            BytesIO(payload),
            mimetype="application/zip",
            as_attachment=True,
            download_name="moxy-contacts-backup.zip",
            max_age=0,
            etag=False,
        )

    @app.route("/archive/restore", methods=["GET", "POST"])
    def restore_backup() -> str | tuple[str, int]:
        if request.method == "GET":
            return render_template("restore.html")
        upload = request.files.get("file")
        if upload is None or not upload.filename or not upload.filename.casefold().endswith(".zip"):
            return render_template("restore.html", error="Choose a Moxy backup .zip file."), 400
        try:
            report = archive().restore_backup(
                upload.read(),
                include_exact_duplicates=request.form.get("include_exact_duplicates") == "yes",
            )
        except ArchiveError as error:
            return render_template("restore.html", error=str(error)), 400
        return render_template("restore_result.html", report=report)

    return app
