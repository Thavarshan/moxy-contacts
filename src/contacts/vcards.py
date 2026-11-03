"""Small, safe helpers around vobject for reading iPhone-style vCards."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, cast

import vobject
from vobject.base import VObjectError

MAX_CARD_BYTES = 1_900_000


class VCardError(ValueError):
    """A user-safe vCard problem that is suitable to show in an import report."""


@dataclass(frozen=True, slots=True)
class VCardIssue:
    entry_number: int
    message: str


@dataclass(frozen=True, slots=True)
class PropertyView:
    name: str
    label: str
    value: str
    group: str | None
    parameters: str


@dataclass(frozen=True, slots=True)
class ParsedVCard:
    original: bytes
    export_bytes: bytes
    display_name: str
    search_text: str
    duplicate_key: str
    properties: tuple[PropertyView, ...]
    photo: bytes | None
    photo_mime_type: str | None

    def properties_named(self, name: str) -> list[PropertyView]:
        """Return every occurrence of a property, preserving the source order."""
        normalized = name.upper()
        return [prop for prop in self.properties if prop.name == normalized]

    def property_values(self, name: str) -> list[str]:
        """Return display-safe values for every occurrence of a property."""
        return [prop.value for prop in self.properties_named(name)]


@dataclass(frozen=True, slots=True)
class VCardUpload:
    cards: tuple[ParsedVCard, ...]
    issues: tuple[VCardIssue, ...]


def parse_vcard(payload: bytes) -> ParsedVCard:
    """Parse exactly one UTF-8 vCard while retaining its exact original bytes."""
    if not payload or len(payload) > MAX_CARD_BYTES:
        raise VCardError("Each contact must be non-empty and 1.9 MB or smaller.")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise VCardError("This contact is not valid UTF-8 text.") from exc

    try:
        components = list(vobject.readComponents(text, validate=False, transform=True))
    except (VObjectError, ValueError, TypeError, StopIteration) as exc:
        raise VCardError("This contact has malformed vCard syntax.") from exc
    if len(components) != 1 or components[0].name.upper() != "VCARD":
        raise VCardError("Upload exactly one complete vCard for each contact.")

    component = cast(Any, components[0])
    lines = list(component.getChildren())
    formatted_name = _first_value(component, "FN")
    if not formatted_name:
        raise VCardError("This contact has no formatted name (FN) property.")

    custom_labels: dict[str, str] = {}
    for line in lines:
        if line.name.upper() == "X-ABLABEL" and line.group:
            custom_labels[line.group.casefold()] = _clean_custom_label(_display_value(line.value))

    properties: list[PropertyView] = []
    searchable: list[str] = []
    emails: list[str] = []
    phones: list[str] = []
    photo: bytes | None = None
    photo_mime_type: str | None = None

    for line in lines:
        name = line.name.upper()
        value = _display_value(line.value)
        parameters = _format_parameters(line.params)
        label = custom_labels.get((line.group or "").casefold()) or _parameter_label(line.params)
        properties.append(
            PropertyView(
                name=name,
                label=label,
                value=value,
                group=line.group,
                parameters=parameters,
            )
        )
        if name not in {"PHOTO", "LOGO", "SOUND", "KEY"} and not isinstance(line.value, bytes):
            searchable.append(value)
        if name == "EMAIL" and isinstance(line.value, str):
            emails.append(_normalize_identity(line.value))
        elif name == "TEL" and isinstance(line.value, str):
            phones.append(_normalize_identity(line.value))
        elif name == "PHOTO" and isinstance(line.value, bytes) and photo is None:
            photo = line.value
            photo_mime_type = _photo_mime_type(line.params)

    normalized_name = _normalize_identity(formatted_name)
    search_text = " ".join(searchable).casefold()
    identities = sorted(set(value for value in (*emails, *phones) if value))
    duplicate_key = (
        hashlib.sha256("\0".join((normalized_name, *identities)).encode("utf-8")).hexdigest()
        if identities
        else ""
    )
    return ParsedVCard(
        original=payload,
        export_bytes=payload,
        display_name=formatted_name,
        search_text=search_text,
        duplicate_key=duplicate_key,
        properties=tuple(properties),
        photo=photo,
        photo_mime_type=photo_mime_type,
    )


def parse_upload(payload: bytes) -> VCardUpload:
    """Parse a multi-card upload and report broken entries without losing good ones."""
    if len(payload) > 12 * 1024 * 1024:
        return VCardUpload((), (VCardIssue(0, "The upload must be 12 MB or smaller."),))

    cards: list[ParsedVCard] = []
    issues: list[VCardIssue] = []
    current: list[bytes] | None = None
    entry_number = 0

    for line in payload.splitlines(keepends=True):
        marker = line.strip().removeprefix(b"\xef\xbb\xbf").upper()
        if marker == b"BEGIN:VCARD":
            if current is not None:
                entry_number += 1
                issues.append(
                    VCardIssue(
                        entry_number, "This contact is incomplete because another one began."
                    )
                )
            current = [line]
        elif marker == b"END:VCARD":
            if current is None:
                issues.append(
                    VCardIssue(entry_number + 1, "Ignored an END:VCARD line outside a contact.")
                )
                continue
            current.append(line)
            entry_number += 1
            try:
                cards.append(parse_vcard(b"".join(current)))
            except VCardError as exc:
                issues.append(VCardIssue(entry_number, str(exc)))
            current = None
        elif current is not None:
            current.append(line)
        elif line.strip():
            issues.append(VCardIssue(entry_number + 1, "Ignored data outside contact records."))

    if current is not None:
        entry_number += 1
        issues.append(
            VCardIssue(entry_number, "This contact is incomplete and has no END:VCARD line.")
        )
    if not cards and not issues:
        issues.append(VCardIssue(1, "No complete BEGIN:VCARD ... END:VCARD record was found."))
    return VCardUpload(tuple(cards), tuple(issues))


def parse_photo(payload: bytes) -> tuple[bytes, str] | None:
    """Return an embedded image only when its MIME type and file signature agree."""
    card = parse_vcard(payload)
    photo = card.photo
    mime_type = card.photo_mime_type
    if photo is None or mime_type is None:
        return None
    signatures = {
        "image/jpeg": photo.startswith(b"\xff\xd8\xff"),
        "image/png": photo.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/gif": photo.startswith((b"GIF87a", b"GIF89a")),
    }
    return (photo, mime_type) if signatures.get(mime_type, False) else None


def sha256(payload: bytes) -> str:
    """Return a stable checksum for duplicate detection and backup verification."""
    return hashlib.sha256(payload).hexdigest()


def _first_value(component: Any, name: str) -> str:
    children = [line for line in component.getChildren() if line.name.upper() == name]
    return _display_value(children[0].value) if children else ""


def _display_value(value: object) -> str:
    if isinstance(value, bytes):
        return f"Embedded image ({len(value)} bytes)"
    if isinstance(value, (list, tuple)):
        return ", ".join(_display_value(item) for item in value)
    return str(value).strip()


def _format_parameters(parameters: dict[str, list[str]]) -> str:
    values = [
        f"{name}={','.join(str(item) for item in items)}"
        for name, items in sorted(parameters.items())
        if name.upper() not in {"ENCODING", "CHARSET"}
    ]
    return "; ".join(values)


def _parameter_label(parameters: dict[str, list[str]]) -> str:
    for name in ("TYPE", "X-ABLABEL"):
        values = parameters.get(name, []) or parameters.get(name.casefold(), [])
        if values:
            return ", ".join(str(value).replace("_", " ").title() for value in values)
    return ""


def _clean_custom_label(value: str) -> str:
    match = re.fullmatch(r"_\$!<(.+)>!\$_", value)
    return match.group(1) if match else value


def _normalize_identity(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _photo_mime_type(parameters: dict[str, list[str]]) -> str | None:
    values = parameters.get("TYPE", []) or parameters.get("type", [])
    if not values:
        return None
    subtype = str(values[0]).casefold()
    return {"jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png", "gif": "image/gif"}.get(
        subtype
    )
