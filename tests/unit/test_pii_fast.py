"""Tests for interlock.pipeline.pii_fast - regex-based PII detection."""

from __future__ import annotations

import pytest

from interlock.models import PIIMatch
from interlock.pipeline.pii_fast import PIIFastScanner

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def scanner() -> PIIFastScanner:
    return PIIFastScanner()


# ---------------------------------------------------------------------------
# Tests: individual pattern types
# ---------------------------------------------------------------------------


def test_detects_ssn(scanner: PIIFastScanner):
    matches = scanner.scan("My SSN is 123-45-6789")
    assert len(matches) == 1
    assert matches[0].entity_type == "SSN"
    assert matches[0].text == "123-45-6789"


def test_detects_credit_card(scanner: PIIFastScanner):
    matches = scanner.scan("Card: 4111-1111-1111-1111")
    cc_matches = [m for m in matches if m.entity_type == "CREDIT_CARD"]
    assert len(cc_matches) == 1
    assert cc_matches[0].text == "4111-1111-1111-1111"


def test_detects_credit_card_no_separators(scanner: PIIFastScanner):
    matches = scanner.scan("Card: 4111111111111111")
    cc_matches = [m for m in matches if m.entity_type == "CREDIT_CARD"]
    assert len(cc_matches) == 1
    assert cc_matches[0].text == "4111111111111111"


def test_detects_email(scanner: PIIFastScanner):
    matches = scanner.scan("Contact me at user@example.com please")
    assert len(matches) == 1
    assert matches[0].entity_type == "EMAIL"
    assert matches[0].text == "user@example.com"


def test_detects_phone_us(scanner: PIIFastScanner):
    matches = scanner.scan("Call me at (555) 123-4567")
    phone_matches = [m for m in matches if m.entity_type == "PHONE"]
    assert len(phone_matches) == 1
    assert "555" in phone_matches[0].text
    assert "4567" in phone_matches[0].text


def test_detects_phone_with_country_code(scanner: PIIFastScanner):
    matches = scanner.scan("Call +1-555-123-4567")
    phone_matches = [m for m in matches if m.entity_type == "PHONE"]
    assert len(phone_matches) == 1


def test_detects_ip_address(scanner: PIIFastScanner):
    matches = scanner.scan("Server at 192.168.1.100")
    ip_matches = [m for m in matches if m.entity_type == "IP_ADDRESS"]
    assert len(ip_matches) == 1
    assert ip_matches[0].text == "192.168.1.100"


def test_detects_ip_address_boundary(scanner: PIIFastScanner):
    """Valid IP addresses only: 0-255 per octet."""
    matches = scanner.scan("Address 999.999.999.999 is invalid")
    ip_matches = [m for m in matches if m.entity_type == "IP_ADDRESS"]
    assert len(ip_matches) == 0


def test_detects_mrn(scanner: PIIFastScanner):
    matches = scanner.scan("Patient MRN:12345678")
    mrn_matches = [m for m in matches if m.entity_type == "MRN"]
    assert len(mrn_matches) == 1
    assert "12345678" in mrn_matches[0].text


def test_detects_mrn_with_space(scanner: PIIFastScanner):
    matches = scanner.scan("Patient MRN 123456")
    mrn_matches = [m for m in matches if m.entity_type == "MRN"]
    assert len(mrn_matches) == 1


# ---------------------------------------------------------------------------
# Tests: no false positives
# ---------------------------------------------------------------------------


def test_no_false_positives_on_normal_text(scanner: PIIFastScanner):
    text = "The quick brown fox jumps over the lazy dog. Today is a good day."
    matches = scanner.scan(text)
    assert len(matches) == 0


def test_no_false_positives_on_short_numbers(scanner: PIIFastScanner):
    text = "I have 42 apples and 100 oranges."
    matches = scanner.scan(text)
    assert len(matches) == 0


# ---------------------------------------------------------------------------
# Tests: scan_row
# ---------------------------------------------------------------------------


def test_scan_row_multiple_fields(scanner: PIIFastScanner):
    row = {
        "name": "John Doe",
        "email": "john@example.com",
        "notes": "SSN is 123-45-6789",
        "age": 30,  # non-string, should be skipped
    }
    results = scanner.scan_row(row)

    assert "email" in results
    assert results["email"][0].entity_type == "EMAIL"

    assert "notes" in results
    assert results["notes"][0].entity_type == "SSN"

    # name has no PII
    assert "name" not in results
    # age is int, not scanned
    assert "age" not in results


def test_scan_row_empty_dict(scanner: PIIFastScanner):
    results = scanner.scan_row({})
    assert results == {}


# ---------------------------------------------------------------------------
# Tests: redact
# ---------------------------------------------------------------------------


def test_redact_replaces_matches(scanner: PIIFastScanner):
    text = "My SSN is 123-45-6789 and email is user@example.com"
    redacted = scanner.redact(text)

    assert "123-45-6789" not in redacted
    assert "user@example.com" not in redacted
    assert "[REDACTED:SSN]" in redacted
    assert "[REDACTED:EMAIL]" in redacted


def test_redact_with_provided_matches(scanner: PIIFastScanner):
    text = "SSN: 123-45-6789"
    matches = [PIIMatch(entity_type="SSN", start=5, end=16, text="123-45-6789")]
    redacted = scanner.redact(text, matches=matches)

    assert redacted == "SSN: [REDACTED:SSN]"


def test_redact_no_matches(scanner: PIIFastScanner):
    text = "Nothing sensitive here"
    redacted = scanner.redact(text)
    assert redacted == text


def test_redact_empty_text(scanner: PIIFastScanner):
    assert scanner.redact("") == ""


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------


def test_multiple_pii_in_same_text(scanner: PIIFastScanner):
    text = "SSN: 111-22-3333, email: a@b.co, phone: 555-123-4567"
    matches = scanner.scan(text)
    types = {m.entity_type for m in matches}
    assert "SSN" in types
    assert "EMAIL" in types
    assert "PHONE" in types


def test_scan_empty_string(scanner: PIIFastScanner):
    assert scanner.scan("") == []


def test_scan_none_like_empty(scanner: PIIFastScanner):
    """scan() with empty string returns empty list."""
    assert scanner.scan("") == []


def test_overlapping_patterns(scanner: PIIFastScanner):
    """Credit card pattern may overlap with phone in edge cases.
    Verify both patterns produce results without errors."""
    # A phone number embedded near a credit-card-like number
    text = "Call 555-123-4567 or pay with 4111-1111-1111-1111"
    matches = scanner.scan(text)
    types = {m.entity_type for m in matches}
    assert "PHONE" in types
    assert "CREDIT_CARD" in types


def test_matches_have_correct_positions(scanner: PIIFastScanner):
    text = "Email: user@test.com"
    matches = scanner.scan(text)
    assert len(matches) == 1
    m = matches[0]
    assert text[m.start : m.end] == "user@test.com"
