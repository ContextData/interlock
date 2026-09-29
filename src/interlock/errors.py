"""Exception hierarchy for the InterLock runtime."""


class InterLockError(Exception):
    """Base exception retained under the internal compatibility namespace."""


# Auth errors
class AuthError(InterLockError):
    """Authentication or authorization failure."""


class IdentityNotFoundError(AuthError):
    """API key does not match any known identity."""


class PolicyDeniedError(AuthError):
    """Request denied by policy engine."""


# Data source errors
class DataSourceError(InterLockError):
    """Error communicating with an upstream data source."""


class DataSourceUnavailableError(DataSourceError):
    """Data source is down or circuit breaker is open."""


class DataSourceNotFoundError(DataSourceError, ValueError):
    """No data source is registered under the requested id.

    A ValueError too, because that is what `ConnectionManager.get_pool` raised
    before it had a type of its own, and existing callers catch it as one.
    """


class ConnectionPoolExhaustedError(DataSourceError):
    """All connections in the pool are in use."""


# Cache errors
class CacheError(InterLockError):
    """Non-fatal cache error - operations should degrade gracefully."""


# SQL errors
class SQLParseError(InterLockError):
    """Failed to parse SQL statement."""


class WriteBlockedError(InterLockError):
    """Write operation blocked by classifier (e.g. EXECUTE/DO blocks)."""


# Write safety errors
class WriteApprovalRequiredError(InterLockError):
    """Write classified as medium/high risk - queued for approval."""

    def __init__(self, approval_id: int, risk_level: str, message: str = "") -> None:
        self.approval_id = approval_id
        self.risk_level = risk_level
        super().__init__(
            message or f"Write queued for approval (id={approval_id}, risk={risk_level})"
        )


# Config errors
class ConfigError(InterLockError):
    """Configuration is invalid or missing."""


class ConfigValidationError(ConfigError):
    """Raised when config.yaml fails schema validation at load time."""


class RuntimeLimitExceededError(InterLockError):
    """A bounded protocol or response limit was exceeded."""


class EgressBlockedError(InterLockError):
    """An outbound destination was rejected by the egress policy."""


class AuditUnavailableError(InterLockError):
    """The configured durable audit sink cannot accept another event."""


class CacheBarrierUnavailableError(CacheError):
    """A write cannot establish the required cross-instance cache barrier."""


class DependencyNotReadyError(InterLockError):
    """A mandatory runtime dependency is not ready to serve traffic."""


# PII errors
class PIIError(InterLockError):
    """Error during PII scanning or redaction."""
