"""Tests for the ResponseProcessor and Redactor."""

from __future__ import annotations

import pytest

from interlock.config import PIIConfig
from interlock.models import PIIMatch
from interlock.pipeline.pii_deep import PIIDeepScanner
from interlock.pipeline.pii_fast import PIIFastScanner
from interlock.pipeline.processor import ResponseProcessor, _merge_matches
from interlock.pipeline.redactor import Redactor

# ---------------------------------------------------------------------------
# Redactor
# ---------------------------------------------------------------------------


class TestRedactor:
    def test_redact_text_single_match(self) -> None:
        text = "SSN is 123-45-6789 here"
        matches = [PIIMatch(entity_type="SSN", start=7, end=18, text="123-45-6789")]
        result = Redactor.redact_text(text, matches)
        assert result == "SSN is [REDACTED:SSN] here"

    def test_redact_text_multiple_matches(self) -> None:
        text = "SSN 123-45-6789 and email user@example.com"
        matches = [
            PIIMatch(entity_type="SSN", start=4, end=15, text="123-45-6789"),
            PIIMatch(entity_type="EMAIL", start=26, end=42, text="user@example.com"),
        ]
        result = Redactor.redact_text(text, matches)
        assert "[REDACTED:SSN]" in result
        assert "[REDACTED:EMAIL]" in result
        assert "123-45-6789" not in result
        assert "user@example.com" not in result

    def test_redact_text_empty_matches(self) -> None:
        text = "No PII here"
        assert Redactor.redact_text(text, []) == "No PII here"

    def test_redact_text_empty_string(self) -> None:
        assert Redactor.redact_text("", []) == ""

    def test_redact_text_custom_template(self) -> None:
        text = "SSN 123-45-6789"
        matches = [PIIMatch(entity_type="SSN", start=4, end=15, text="123-45-6789")]
        result = Redactor.redact_text(text, matches, replacement_template="***{entity_type}***")
        assert result == "SSN ***SSN***"

    def test_redact_row(self) -> None:
        row = {"name": "Alice", "ssn": "123-45-6789", "age": 30}
        field_matches = {"ssn": [PIIMatch(entity_type="SSN", start=0, end=11, text="123-45-6789")]}
        redacted = Redactor.redact_row(row, field_matches)
        assert redacted["ssn"] == "[REDACTED:SSN]"
        assert redacted["name"] == "Alice"
        assert redacted["age"] == 30
        # Original row unchanged
        assert row["ssn"] == "123-45-6789"

    def test_redact_row_no_matches(self) -> None:
        row = {"name": "Alice", "age": 30}
        redacted = Redactor.redact_row(row, {})
        assert redacted == row


# ---------------------------------------------------------------------------
# _merge_matches helper
# ---------------------------------------------------------------------------


class TestMergeMatches:
    def test_merge_disjoint(self) -> None:
        fast = {"f1": [PIIMatch(entity_type="SSN", start=0, end=11, text="123-45-6789")]}
        deep = {"f2": [PIIMatch(entity_type="EMAIL", start=0, end=16, text="user@example.com")]}
        merged = _merge_matches(fast, deep)
        assert "f1" in merged
        assert "f2" in merged

    def test_merge_deduplicates(self) -> None:
        match = PIIMatch(entity_type="SSN", start=0, end=11, text="123-45-6789")
        fast = {"f1": [match]}
        deep = {"f1": [match]}
        merged = _merge_matches(fast, deep)
        assert len(merged["f1"]) == 1

    def test_merge_empty(self) -> None:
        assert _merge_matches({}, {}) == {}


# ---------------------------------------------------------------------------
# ResponseProcessor
# ---------------------------------------------------------------------------


class TestResponseProcessor:
    @pytest.fixture
    def fast_scanner(self) -> PIIFastScanner:
        return PIIFastScanner()

    @pytest.fixture
    def processor(self, fast_scanner: PIIFastScanner) -> ResponseProcessor:
        return ResponseProcessor(fast_scanner=fast_scanner)

    @pytest.mark.asyncio
    async def test_process_row_fast_only(self, processor: ResponseProcessor) -> None:
        row = {"name": "Alice", "ssn": "123-45-6789"}
        redacted, matches = await processor.process_row(row)
        assert "123-45-6789" not in redacted["ssn"]
        assert "[REDACTED:SSN]" in redacted["ssn"]
        assert len(matches) == 1
        assert matches[0].entity_type == "SSN"

    @pytest.mark.asyncio
    async def test_process_row_detects_and_redacts_ssn(self, processor: ResponseProcessor) -> None:
        row = {"id": "1", "notes": "Patient SSN is 987-65-4321"}
        redacted, matches = await processor.process_row(row)
        assert "987-65-4321" not in redacted["notes"]
        assert any(m.entity_type == "SSN" for m in matches)

    @pytest.mark.asyncio
    async def test_process_row_no_pii(self, processor: ResponseProcessor) -> None:
        row = {"name": "Alice", "city": "Portland", "age": 30}
        redacted, matches = await processor.process_row(row)
        assert redacted["name"] == "Alice"
        assert redacted["city"] == "Portland"
        assert redacted["age"] == 30
        assert matches == []

    @pytest.mark.asyncio
    async def test_process_rows_multiple(self, processor: ResponseProcessor) -> None:
        rows = [
            {"name": "Alice", "ssn": "123-45-6789"},
            {"name": "Bob", "city": "Portland"},
        ]
        redacted_rows, pii_detected = await processor.process_rows(rows)
        assert len(redacted_rows) == 2
        assert pii_detected is True
        assert "[REDACTED:SSN]" in redacted_rows[0]["ssn"]
        assert redacted_rows[1]["city"] == "Portland"

    @pytest.mark.asyncio
    async def test_process_rows_no_pii(self, processor: ResponseProcessor) -> None:
        rows = [
            {"name": "Alice", "city": "Portland"},
            {"name": "Bob", "city": "Seattle"},
        ]
        redacted_rows, pii_detected = await processor.process_rows(rows)
        assert pii_detected is False
        assert redacted_rows[0] == rows[0]

    @pytest.mark.asyncio
    async def test_process_rows_returns_pii_detected_true(
        self, processor: ResponseProcessor
    ) -> None:
        rows = [{"email_field": "contact me at user@example.com"}]
        _, pii_detected = await processor.process_rows(rows)
        assert pii_detected is True

    @pytest.mark.asyncio
    async def test_process_row_with_deep_scanner_unavailable(
        self, fast_scanner: PIIFastScanner
    ) -> None:
        deep = PIIDeepScanner()
        await deep.initialize()  # Will be unavailable without Presidio
        processor = ResponseProcessor(fast_scanner=fast_scanner, deep_scanner=deep)
        row = {
            "description": "Long text with SSN 111-22-3333 embedded in it for testing deep scanning capability."
        }
        redacted, matches = await processor.process_row(row)
        # Fast scanner still catches the SSN
        assert any(m.entity_type == "SSN" for m in matches)
        assert "111-22-3333" not in redacted["description"]

    @pytest.mark.asyncio
    async def test_process_row_with_deep_scanner_redacts_contextual_match(
        self, fast_scanner: PIIFastScanner
    ) -> None:
        class FakeDeepScanner:
            available = True

            async def scan(self, text: str) -> list[PIIMatch]:
                start = text.index("Alice Example")
                return [
                    PIIMatch(
                        entity_type="PERSON",
                        start=start,
                        end=start + len("Alice Example"),
                        text="Alice Example",
                    )
                ]

        processor = ResponseProcessor(
            fast_scanner=fast_scanner,
            deep_scanner=FakeDeepScanner(),  # type: ignore[arg-type]
            config=PIIConfig(deep_enabled=True),
        )
        row = {
            "description": (
                "Escalation notes for Alice Example include a non-regex "
                "identifier that the deep scanner should catch."
            )
        }

        redacted, matches = await processor.process_row(row)

        assert "Alice Example" not in redacted["description"]
        assert "[REDACTED:PERSON]" in redacted["description"]
        assert any(match.entity_type == "PERSON" for match in matches)

    @pytest.mark.asyncio
    async def test_process_row_deep_scanner_disabled_skips_contextual_scan(
        self, fast_scanner: PIIFastScanner
    ) -> None:
        class FakeDeepScanner:
            available = True
            called = False

            async def scan(self, text: str) -> list[PIIMatch]:
                self.called = True
                return []

        deep = FakeDeepScanner()
        processor = ResponseProcessor(
            fast_scanner=fast_scanner,
            deep_scanner=deep,  # type: ignore[arg-type]
            config=PIIConfig(deep_enabled=False),
        )

        redacted, matches = await processor.process_row(
            {"description": "Long free text with no regex PII but enough content for deep scan."}
        )

        assert deep.called is False
        assert matches == []
        assert redacted["description"].startswith("Long free text")

    @pytest.mark.asyncio
    async def test_process_row_honors_fast_scanner_disabled(
        self, fast_scanner: PIIFastScanner
    ) -> None:
        processor = ResponseProcessor(
            fast_scanner=fast_scanner,
            config=PIIConfig(fast_enabled=False, deep_enabled=False),
        )

        redacted, matches = await processor.process_row({"ssn": "123-45-6789"})

        assert redacted["ssn"] == "123-45-6789"
        assert matches == []

    @pytest.mark.asyncio
    async def test_process_row_uses_configured_free_text_patterns(
        self, fast_scanner: PIIFastScanner
    ) -> None:
        class FakeDeepScanner:
            available = True

            async def scan(self, text: str) -> list[PIIMatch]:
                start = text.index("Project Falcon")
                return [
                    PIIMatch(
                        entity_type="PROJECT_CODE",
                        start=start,
                        end=start + len("Project Falcon"),
                        text="Project Falcon",
                    )
                ]

        processor = ResponseProcessor(
            fast_scanner=fast_scanner,
            deep_scanner=FakeDeepScanner(),  # type: ignore[arg-type]
            config=PIIConfig(deep_enabled=True, free_text_column_patterns=["case_blob"]),
        )

        redacted, matches = await processor.process_row(
            {"case_blob": "Internal note for Project Falcon should be contextual PII."}
        )

        assert "Project Falcon" not in redacted["case_blob"]
        assert any(match.entity_type == "PROJECT_CODE" for match in matches)

    @pytest.mark.asyncio
    async def test_process_row_deep_scanner_failure_redacts_free_text(
        self, fast_scanner: PIIFastScanner
    ) -> None:
        class FailingDeepScanner:
            available = True

            async def scan(self, text: str) -> list[PIIMatch]:
                raise RuntimeError("deep scanner unavailable mid-request")

        processor = ResponseProcessor(
            fast_scanner=fast_scanner,
            deep_scanner=FailingDeepScanner(),  # type: ignore[arg-type]
            config=PIIConfig(deep_enabled=True),
        )
        row = {
            "description": "This free-text field must not be returned if deep scanning fails.",
            "status": "open",
        }

        redacted, matches = await processor.process_row(row)

        assert redacted["description"] == "[REDACTED:PII_SCAN_FAILED]"
        assert redacted["status"] == "open"
        assert any(match.entity_type == "PII_SCAN_FAILED" for match in matches)

    @pytest.mark.asyncio
    async def test_process_row_deep_scanner_failure_preserves_fast_redactions(
        self, fast_scanner: PIIFastScanner
    ) -> None:
        class FailingDeepScanner:
            available = True

            async def scan(self, text: str) -> list[PIIMatch]:
                raise RuntimeError("deep scanner unavailable mid-request")

        processor = ResponseProcessor(
            fast_scanner=fast_scanner,
            deep_scanner=FailingDeepScanner(),  # type: ignore[arg-type]
            config=PIIConfig(deep_enabled=True),
        )
        row = {
            "ssn": "My SSN is 123-45-6789",
            "description": "This free-text field must not be returned if deep scanning fails.",
        }

        redacted, matches = await processor.process_row(row)

        assert "123-45-6789" not in redacted["ssn"]
        assert redacted["description"] == "[REDACTED:PII_SCAN_FAILED]"
        assert any(match.entity_type == "SSN" for match in matches)
        assert any(match.entity_type == "PII_SCAN_FAILED" for match in matches)

    @pytest.mark.asyncio
    async def test_process_row_fast_scanner_failure_redacts_all_string_fields(
        self,
    ) -> None:
        class FailingFastScanner:
            def scan(self, text: str) -> list[PIIMatch]:
                raise RuntimeError("fast scanner failed")

        processor = ResponseProcessor(
            fast_scanner=FailingFastScanner(),  # type: ignore[arg-type]
            config=PIIConfig(deep_enabled=False),
        )
        row = {"name": "Alice", "nested": {"email": "alice@example.com"}, "age": 30}

        redacted, matches = await processor.process_row(row)

        assert redacted["name"] == "[REDACTED:PII_SCAN_FAILED]"
        assert redacted["nested"]["email"] == "[REDACTED:PII_SCAN_FAILED]"
        assert redacted["age"] == 30
        assert len(matches) == 2

    @pytest.mark.asyncio
    async def test_process_row_redacts_nested_json_strings(
        self, processor: ResponseProcessor
    ) -> None:
        row = {
            "profile": {
                "contact": {
                    "email": "alice@example.com",
                    "phones": ["Call 415-555-1212"],
                }
            }
        }

        redacted, matches = await processor.process_row(row)

        assert "alice@example.com" not in redacted["profile"]["contact"]["email"]
        assert "415-555-1212" not in redacted["profile"]["contact"]["phones"][0]
        assert {match.entity_type for match in matches} >= {"EMAIL", "PHONE"}
