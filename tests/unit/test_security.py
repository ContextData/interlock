"""Unit tests for the SecurityScanner."""

from __future__ import annotations

from interlock.config import InterLockConfig
from interlock.utils.security import SecurityFinding, SecurityScanner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(**overrides) -> InterLockConfig:
    """Build an InterLockConfig with optional nested overrides.

    Keys use dot notation: "database.password" -> database={"password": ...}
    """
    nested: dict = {}
    for dotted_key, value in overrides.items():
        parts = dotted_key.split(".")
        target = nested
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    return InterLockConfig(**nested)


def _severities(findings: list[SecurityFinding]) -> list[str]:
    return [f.severity for f in findings]


def _categories(findings: list[SecurityFinding]) -> list[str]:
    return [f.category for f in findings]


def _descriptions(findings: list[SecurityFinding]) -> list[str]:
    return [f.description for f in findings]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSecurityScannerAuthDisabled:
    def test_detects_auth_disabled(self):
        cfg = _config(**{"auth.enabled": False})
        scanner = SecurityScanner(cfg)
        findings = scanner.scan_all()
        auth_findings = [f for f in findings if f.category == "auth"]
        assert any(f.severity == "critical" for f in auth_findings)
        assert any("disabled" in f.description.lower() for f in auth_findings)


class TestSecurityScannerEmptyDBPassword:
    def test_detects_empty_password(self):
        cfg = _config(**{"database.password": ""})
        scanner = SecurityScanner(cfg)
        findings = scanner.scan_all()
        config_findings = [f for f in findings if f.category == "config"]
        assert any(f.severity == "critical" for f in config_findings)

    def test_detects_default_password(self):
        cfg = _config(**{"database.password": "changeme"})
        scanner = SecurityScanner(cfg)
        findings = scanner.scan_all()
        config_findings = [f for f in findings if f.category == "config"]
        assert any(f.severity == "high" for f in config_findings)


class TestSecurityScannerPIIDisabled:
    def test_detects_pii_fast_disabled(self):
        cfg = _config(**{"pii.fast_enabled": False})
        scanner = SecurityScanner(cfg)
        findings = scanner.scan_all()
        pii_findings = [f for f in findings if f.category == "pii"]
        assert any(f.severity == "high" for f in pii_findings)
        assert any("fast" in f.description.lower() for f in pii_findings)


class TestSecurityScannerCleanConfig:
    def test_clean_config_has_only_low_or_info(self):
        """A 'clean' config should produce no critical/high findings."""
        cfg = _config(
            **{
                "auth.enabled": True,
                "auth.session_ttl_seconds": 3600,
                "database.password": "a-very-strong-production-password-1234",
                "redis.url": "rediss://:secret@redis:6379/0",
                "pii.fast_enabled": True,
                "pii.deep_enabled": True,
            }
        )
        scanner = SecurityScanner(cfg)
        findings = scanner.scan_all()
        serious = [f for f in findings if f.severity in ("critical", "high")]
        assert serious == [], f"Unexpected serious findings: {serious}"


class TestSecurityFindingModel:
    def test_finding_has_correct_fields(self):
        f = SecurityFinding(
            severity="high",
            category="auth",
            description="test desc",
            recommendation="test rec",
        )
        assert f.severity == "high"
        assert f.category == "auth"
        assert f.description == "test desc"
        assert f.recommendation == "test rec"

    def test_finding_serialization(self):
        f = SecurityFinding(
            severity="critical",
            category="secrets",
            description="d",
            recommendation="r",
        )
        data = f.model_dump()
        assert data["severity"] == "critical"
        assert data["category"] == "secrets"
