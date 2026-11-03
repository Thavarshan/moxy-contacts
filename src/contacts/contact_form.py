"""Translate a small structured contact form to and from common vCard fields."""

from __future__ import annotations

from typing import Any, cast

import vobject
from werkzeug.datastructures import MultiDict

EMAIL_TYPES = {"home": "HOME", "work": "WORK", "other": ""}
PHONE_TYPES = {"home": "HOME", "work": "WORK", "mobile": "CELL", "other": ""}
MAX_REPEAT_FIELDS = 20


class ContactFormError(ValueError):
    """A safe message for invalid structured contact input."""


def empty_contact_form_values() -> dict[str, object]:
    return {
        "prefix": "",
        "given_name": "",
        "additional_name": "",
        "family_name": "",
        "suffix": "",
        "organization": "",
        "title": "",
        "emails": [{"value": "", "type": "work"}],
        "phones": [{"value": "", "type": "mobile"}],
        "website": "",
        "note": "",
    }


def contact_form_values_from_post(form: MultiDict[str, str]) -> dict[str, object]:
    values = empty_contact_form_values()
    for key in ("prefix", "given_name", "additional_name", "family_name", "suffix"):
        values[key] = form.get(key, "")
    for key in ("organization", "title", "website", "note"):
        values[key] = form.get(key, "")
    values["emails"] = _posted_rows(form, "email", "email_type", EMAIL_TYPES)
    values["phones"] = _posted_rows(form, "phone", "phone_type", PHONE_TYPES)
    return values


def contact_form_values_from_vcard(payload: bytes) -> dict[str, object]:
    values = empty_contact_form_values()
    card = vobject.readOne(payload.decode("utf-8-sig"), validate=False, transform=True)

    name_lines = card.contents.get("n", [])
    if name_lines:
        name = name_lines[0].value
        for key, attribute in (
            ("prefix", "prefix"),
            ("given_name", "given"),
            ("additional_name", "additional"),
            ("family_name", "family"),
            ("suffix", "suffix"),
        ):
            values[key] = _display_field(getattr(name, attribute, ""))
    if not values["given_name"] and not values["family_name"]:
        formatted_names = card.contents.get("fn", [])
        if formatted_names:
            values["given_name"] = _display_field(formatted_names[0].value)

    for key, property_name in (
        ("organization", "org"),
        ("title", "title"),
        ("website", "url"),
        ("note", "note"),
    ):
        lines = card.contents.get(property_name, [])
        if lines:
            values[key] = _display_field(lines[0].value)

    email_rows = [
        {"value": _display_field(line.value), "type": _email_type(line)}
        for line in card.contents.get("email", [])
    ]
    phone_rows = [
        {"value": _display_field(line.value), "type": _phone_type(line)}
        for line in card.contents.get("tel", [])
    ]
    values["emails"] = email_rows or values["emails"]
    values["phones"] = phone_rows or values["phones"]
    return values


def build_contact_vcard(form: MultiDict[str, str], *, existing_vcard: bytes | None = None) -> bytes:
    """Create a vCard from visible fields, preserving other fields when editing."""
    prefix = _single_line(form, "prefix", "Name prefix", 128)
    given = _single_line(form, "given_name", "First name", 256)
    additional = _single_line(form, "additional_name", "Middle name", 256)
    family = _single_line(form, "family_name", "Last name", 256)
    suffix = _single_line(form, "suffix", "Name suffix", 128)
    organization = _single_line(form, "organization", "Organization", 512)
    title = _single_line(form, "title", "Job title", 512)
    website = _single_line(form, "website", "Website", 2048)
    note = _multiline(form, "note", "Notes", 4096)

    if not given and not family:
        raise ContactFormError("Enter a first name or a last name.")

    email_values = form.getlist("email")
    email_types = form.getlist("email_type")
    phone_values = form.getlist("phone")
    phone_types = form.getlist("phone_type")
    if len(email_values) > MAX_REPEAT_FIELDS or len(phone_values) > MAX_REPEAT_FIELDS:
        raise ContactFormError(f"Use no more than {MAX_REPEAT_FIELDS} emails or phone numbers.")

    emails = [
        (
            _bounded_value(value, "Email address", 320, multiline=False),
            _choice(email_types, i, EMAIL_TYPES),
        )
        for i, value in enumerate(email_values)
    ]
    phones = [
        (
            _bounded_value(value, "Phone number", 128, multiline=False),
            _choice(phone_types, i, PHONE_TYPES),
        )
        for i, value in enumerate(phone_values)
    ]
    emails = [(value, kind) for value, kind in emails if value]
    phones = [(value, kind) for value, kind in phones if value]

    if existing_vcard is None:
        card = vobject.vCard()
    else:
        card = vobject.readOne(existing_vcard.decode("utf-8-sig"), validate=False, transform=True)

    replaced_groups = {
        line.group.casefold()
        for property_name in ("email", "tel")
        for line in card.contents.get(property_name, [])
        if line.group
    }
    for property_name in ("fn", "n", "org", "title", "email", "tel", "url", "note"):
        for line in list(card.contents.get(property_name, [])):
            card.remove(line)
    for line in list(card.contents.get("x-ablabel", [])):
        if line.group and line.group.casefold() in replaced_groups:
            card.remove(line)

    formatted_name = " ".join(part for part in (prefix, given, additional, family, suffix) if part)
    card.add("fn").value = formatted_name
    card.add("n").value = vobject.vcard.Name(
        family=family,
        given=given,
        additional=additional,
        prefix=prefix,
        suffix=suffix,
    )
    if organization:
        card.add("org").value = [organization]
    if title:
        card.add("title").value = title
    for value, kind in emails:
        line = card.add("email")
        line.value = value
        _set_type(line, kind, EMAIL_TYPES)
    for value, kind in phones:
        line = card.add("tel")
        line.value = value
        _set_type(line, kind, PHONE_TYPES)
    if website:
        card.add("url").value = website
    if note:
        card.add("note").value = note

    serialized = cast(str, card.serialize(validate=False))
    return serialized.encode("utf-8")


def _posted_rows(
    form: MultiDict[str, str], value_field: str, type_field: str, allowed: dict[str, str]
) -> list[dict[str, str]]:
    values = form.getlist(value_field)[:MAX_REPEAT_FIELDS]
    kinds = form.getlist(type_field)
    rows = [{"value": value, "type": _choice(kinds, i, allowed)} for i, value in enumerate(values)]
    return rows or [{"value": "", "type": "other"}]


def _single_line(form: MultiDict[str, str], key: str, label: str, limit: int) -> str:
    return _bounded_value(form.get(key, ""), label, limit, multiline=False)


def _multiline(form: MultiDict[str, str], key: str, label: str, limit: int) -> str:
    return _bounded_value(form.get(key, ""), label, limit, multiline=True)


def _bounded_value(value: str, label: str, limit: int, *, multiline: bool) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.strip() if multiline else " ".join(normalized.split())
    if len(normalized.encode("utf-8")) > limit:
        raise ContactFormError(f"{label} must be {limit} bytes or fewer.")
    return normalized


def _choice(values: list[str], index: int, allowed: dict[str, str]) -> str:
    choice = values[index].casefold() if index < len(values) else "other"
    return choice if choice in allowed else "other"


def _set_type(line: Any, kind: str, allowed: dict[str, str]) -> None:
    parameter = allowed.get(kind, "")
    if parameter:
        line.params["TYPE"] = [parameter]


def _display_field(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(part) for part in value if part).strip()
    return str(value or "").strip()


def _parameter_values(line: Any) -> set[str]:
    raw = line.params.get("TYPE", [])
    if isinstance(raw, str):
        return {raw.upper()}
    return {str(value).upper() for value in raw}


def _email_type(line: Any) -> str:
    values = _parameter_values(line)
    if "WORK" in values:
        return "work"
    if "HOME" in values:
        return "home"
    return "other"


def _phone_type(line: Any) -> str:
    values = _parameter_values(line)
    if "CELL" in values or "MOBILE" in values:
        return "mobile"
    if "WORK" in values:
        return "work"
    if "HOME" in values:
        return "home"
    return "other"
