"""Security scanner - audits InterLock configuration for common vulnerabilities.

Status: implemented and unit-tested, but NOT wired into the live
request path. See the "Code Present But Not On The Request Path" table in
docs-site/src/content/docs/reference/feature-status.md before treating this as current behavior.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from interlock.config import InterLockConfig


class SecurityFinding(BaseModel):
    """A single finding from a security scan."""

    severity: str  # "critical", "high", "medium", "low", "info"
    category: str  # "auth", "encryption", "pii", "config", "secrets"
    description: str
    recommendation: str


# Default passwords / weak values that should never appear in production
_WEAK_PASSWORDS = frozenset(
    {
        "",
        "password",
        "postgres",
        "admin",
        "changeme",
        "secret",
        "onyx",
        "onyx_dev",
        "test",
        "default",
        "root",
        "123456",
    }
)


class SecurityScanner:
    """Scans the InterLock configuration for security issues.

    Intended to run at startup or on-demand via admin API to surface
    misconfigurations before they reach production.
    """

    def __init__(self, config: InterLockConfig) -> None:
        self._config = config
        self._findings: list[SecurityFinding] = []

    def scan_all(self) -> list[SecurityFinding]:
        """Run all security checks and return findings."""
        self._findings.clear()
        self._check_auth_config()
        self._check_database_config()
        self._check_redis_config()
        self._check_pii_config()
        self._check_tls_config()
        self._check_secrets()
        return list(self._findings)

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_auth_config(self) -> None:
        """Check auth is enabled and session TTL is reasonable."""
        auth = self._config.auth

        if not auth.enabled:
            self._add(
                severity="critical",
                category="auth",
                description="Authentication is disabled. All requests will bypass identity checks.",
                recommendation="Set auth.enabled = true in production.",
            )
            return

        if auth.session_ttl_seconds > 86400:
            self._add(
                severity="medium",
                category="auth",
                description=(
                    f"Session TTL is {auth.session_ttl_seconds}s (>{86400}s / 24h). "
                    "Long-lived sessions increase the window for stolen credentials."
                ),
                recommendation="Reduce session_ttl_seconds to 3600 (1h) or less.",
            )

        if auth.session_ttl_seconds < 60:
            self._add(
                severity="low",
                category="auth",
                description=(
                    f"Session TTL is very short ({auth.session_ttl_seconds}s). "
                    "This may cause excessive re-authentication."
                ),
                recommendation="Consider a TTL of at least 300s (5 min).",
            )

    def _check_database_config(self) -> None:
        """Check DB password strength and connection parameters."""
        db = self._config.database

        if db.password in _WEAK_PASSWORDS:
            severity = "critical" if db.password == "" else "high"
            self._add(
                severity=severity,
                category="config",
                description=("Database password is empty or a well-known default value."),
                recommendation=("Set a strong, unique password for the database user."),
            )

    def _check_redis_config(self) -> None:
        """Check Redis URL for authentication."""
        url = self._config.redis.url

        # redis://host:port with no userinfo means no auth
        if re.match(r"^rediss?://[^@]+:\d+", url) and "@" not in url:
            self._add(
                severity="medium",
                category="config",
                description="Redis connection has no authentication configured.",
                recommendation=("Use redis://:password@host:port or enable Redis ACLs."),
            )

    def _check_pii_config(self) -> None:
        """Check PII scanning is enabled."""
        pii = self._config.pii

        if not pii.fast_enabled:
            self._add(
                severity="high",
                category="pii",
                description="Fast PII scanning is disabled. Sensitive data may pass through undetected.",
                recommendation="Set pii.fast_enabled = true.",
            )

        if not pii.deep_enabled:
            self._add(
                severity="info",
                category="pii",
                description="Deep PII scanning is disabled. Only regex-based detection is active.",
                recommendation=(
                    "Consider enabling pii.deep_enabled for ML-based detection "
                    "of contextual PII."
                ),
            )

    def _check_tls_config(self) -> None:
        """Warn if TLS is not configured for external services."""
        redis_url = self._config.redis.url
        if redis_url.startswith("redis://"):
            self._add(
                severity="low",
                category="encryption",
                description="Redis connection does not use TLS (redis:// instead of rediss://).",
                recommendation="Use rediss:// in production for encrypted Redis traffic.",
            )

    def _check_secrets(self) -> None:
        """Check for common secret exposure patterns."""
        db = self._config.database

        # Password appearing in plain-text connection strings is inherent,
        # but we flag extremely short passwords as likely placeholders.
        if db.password and len(db.password) < 8:
            self._add(
                severity="medium",
                category="secrets",
                description=(
                    f"Database password is only {len(db.password)} characters. "
                    "Short passwords are easy to brute-force."
                ),
                recommendation="Use a password of at least 16 characters.",
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _add(
        self,
        severity: str,
        category: str,
        description: str,
        recommendation: str,
    ) -> None:
        self._findings.append(
            SecurityFinding(
                severity=severity,
                category=category,
                description=description,
                recommendation=recommendation,
            )
        )
