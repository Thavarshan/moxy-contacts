"""Synthetic iPhone-style cards exercise vCard parsing without personal data."""

from pathlib import Path

import pytest

from contacts.vcards import MAX_CARD_BYTES, VCardError, parse_upload, parse_vcard

FIXTURES = Path(__file__).parent / "fixtures"
APPLE_STYLE = (FIXTURES / "iphone_style.vcf").read_bytes()


def test_parse_preserves_original_and_exposes_rich_repeated_properties() -> None:
    card = parse_vcard(APPLE_STYLE)

    assert card.original == APPLE_STYLE
    assert card.display_name == "Dr. Zoë S. Koda"
    assert card.property_values("TEL") == ["+1-555-0100", "+1-555-0101"]
    assert card.property_values("EMAIL") == ["zoe@example.test", "zoe.work@example.test"]
    assert card.property_values("X-ABRELATEDNAMES") == ["Sam Example"]
    assert card.property_values("X-APPLE-OMIT-YEAR") == ["1"]
    assert "data science" in card.search_text
    assert "z ✨" in card.search_text


def test_parse_resolves_apple_custom_labels_and_structured_values() -> None:
    card = parse_vcard(APPLE_STYLE)

    phone = card.properties_named("TEL")[0]
    address = card.properties_named("ADR")[0]

    assert phone.label == "Home"
    assert address.label == "Office"
    assert "Colombo" in address.value
    assert "Sri Lanka" in address.value


def test_parse_handles_folded_lines_escaped_text_and_unicode() -> None:
    folded = (
        "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Example;Zoë;;;;\r\n"
        "FN:Zoë Example with a folded\r\n line\r\n"
        "NOTE:escaped\\, comma and newline\\nline two\r\nEND:VCARD\r\n"
    ).encode()

    card = parse_vcard(folded)

    assert card.display_name == "Zoë Example with a foldedline"
    assert card.property_values("NOTE") == ["escaped, comma and newline\nline two"]


def test_upload_reports_valid_cards_and_each_malformed_entry() -> None:
    first = b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:First Example\r\nEND:VCARD\r\n"
    broken = b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Broken Example\r\n"
    third = b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Third Example\r\nEND:VCARD\r\n"

    result = parse_upload(first + broken + third)

    assert [card.display_name for card in result.cards] == ["First Example", "Third Example"]
    assert len(result.issues) == 1
    assert result.issues[0].entry_number == 2
    assert "incomplete" in result.issues[0].message.casefold()


def test_upload_reports_non_vcard_data_without_stopping_other_cards() -> None:
    card = b"BEGIN:VCARD\nVERSION:3.0\nFN:Kept Example\nEND:VCARD\n"

    result = parse_upload(b"not a contact\n" + card)

    assert len(result.cards) == 1
    assert result.cards[0].display_name == "Kept Example"
    assert result.issues
    assert "outside" in result.issues[0].message.casefold()


def test_parse_rejects_invalid_utf8_missing_fn_and_oversized_card() -> None:
    with pytest.raises(VCardError, match="UTF-8"):
        parse_vcard(b"BEGIN:VCARD\nVERSION:3.0\nFN:\xff\nEND:VCARD\n")
    with pytest.raises(VCardError, match="formatted name"):
        parse_vcard(b"BEGIN:VCARD\nVERSION:3.0\nEND:VCARD\n")
    with pytest.raises(VCardError, match="1.9 MB"):
        parse_vcard(b"x" * (MAX_CARD_BYTES + 1))


def test_export_bytes_can_be_parsed_again_with_repeated_custom_fields() -> None:
    card = parse_vcard(APPLE_STYLE)

    reparsed = parse_vcard(card.export_bytes)

    assert reparsed.display_name == card.display_name
    assert reparsed.property_values("TEL") == card.property_values("TEL")
    assert reparsed.property_values("X-CUSTOM-PROPERTY") == ["Keep this unknown value"]
    assert reparsed.properties_named("TEL")[0].label == "Home"
