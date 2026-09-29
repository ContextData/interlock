"""Tests for the deep PII scanner (Presidio-backed, gracefully degrading)."""

from __future__ import annotations

import builtins

import pytest

from interlock.pipeline.pii_deep import PIIDeepScanError, PIIDeepScanner, is_free_text_column

# ---------------------------------------------------------------------------
# is_free_text_column
# ---------------------------------------------------------------------------


class TestIsFreeTextColumn:
    @pytest.mark.parametrize(
        "name",
        [
            "description",
            "user_notes",
            "comment_body",
            "bio",
            "message",
            "body",
            "content",
            "remarks",
            "summary",
            "text",
            "detail",
            "narrative",
            "memo",
            "review",
            "feedback",
            "customer_description",
            "DESCRIPTION",
            "UserNotes",
        ],
    )
    def test_matching_names(self, name: str) -> None:
        assert is_free_text_column(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "id",
            "user_id",
            "created_at",
            "email",
            "phone",
            "name",
            "age",
            "status",
            "price",
            "count",
        ],
    )
    def test_non_matching_names(self, name: str) -> None:
        assert is_free_text_column(name) is False


# ---------------------------------------------------------------------------
# PIIDeepScanner (without Presidio installed)
# ---------------------------------------------------------------------------


class TestPIIDeepScannerUnavailable:
    """Unavailable behavior is simulated so the test is stable with all extras."""

    @pytest.fixture(autouse=True)
    def _hide_presidio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original_import = builtins.__import__

        def guarded_import(name: str, *args, **kwargs):
            if name in {"presidio_analyzer", "spacy"}:
                raise ImportError(name)
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", guarded_import)

    @pytest.mark.asyncio
    async def test_initialize_without_presidio(self) -> None:
        scanner = PIIDeepScanner()
        await scanner.initialize()
        # Should not raise, just mark itself unavailable
        assert scanner.available is False

    @pytest.mark.asyncio
    async def test_scan_when_unavailable(self) -> None:
        scanner = PIIDeepScanner()
        await scanner.initialize()
        result = await scanner.scan(
            "This is a long text with SSN 123-45-6789 embedded in it for testing purposes."
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_scan_fields_when_unavailable(self) -> None:
        scanner = PIIDeepScanner()
        await scanner.initialize()
        fields = {
            "description": "A long description field that has SSN 123-45-6789 in it for testing.",
            "user_id": "12345",
        }
        result = await scanner.scan_fields(fields)
        assert result == {}

    @pytest.mark.asyncio
    async def test_scan_fields_filters_non_free_text(self) -> None:
        """Even if the scanner were available, non-free-text fields are skipped."""
        scanner = PIIDeepScanner()
        # Manually set available to test filtering logic
        scanner._available = True
        # But no executor, so scan() will return empty
        fields = {
            "user_id": "Some long text that is definitely more than fifty characters long here.",
            "email": "Some long text that is definitely more than fifty characters long here.",
        }
        result = await scanner.scan_fields(fields)
        assert result == {}

    @pytest.mark.asyncio
    async def test_shutdown_is_safe(self) -> None:
        scanner = PIIDeepScanner()
        await scanner.initialize()
        await scanner.shutdown()
        assert scanner.available is False

    @pytest.mark.asyncio
    async def test_shutdown_without_initialize(self) -> None:
        scanner = PIIDeepScanner()
        await scanner.shutdown()  # Should not raise
        assert scanner.available is False

    def test_default_available_false(self) -> None:
        scanner = PIIDeepScanner()
        assert scanner.available is False

    @pytest.mark.asyncio
    async def test_scan_short_text_skipped(self) -> None:
        """Text at or below MIN_TEXT_LENGTH is skipped even if available."""
        scanner = PIIDeepScanner()
        scanner._available = True
        # Short text - should be skipped before hitting executor
        result = await scanner.scan("short")
        assert result == []

    @pytest.mark.asyncio
    async def test_scan_failure_raises_typed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FailingExecutor:
            pass

        class FakeLoop:
            async def run_in_executor(self, executor, func):
                raise RuntimeError("worker failed")

        scanner = PIIDeepScanner()
        scanner._available = True
        scanner._executor = FailingExecutor()  # type: ignore[assignment]
        monkeypatch.setattr("asyncio.get_running_loop", lambda: FakeLoop())

        with pytest.raises(PIIDeepScanError):
            await scanner.scan(
                "This long text is definitely above the minimum length for deep scanning."
            )
