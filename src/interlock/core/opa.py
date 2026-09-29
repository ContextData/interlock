"""Open Policy Agent integration for externalized authorization.

Provides an OPA client that can supplement or replace the built-in RBAC
PolicyEngine. When OPA is unreachable and fallback_to_builtin is True,
evaluate() returns None so the caller can fall back to the built-in engine.

Status: implemented and unit-tested, but NOT wired into the live
request path. See the "Code Present But Not On The Request Path" table in
docs-site/src/content/docs/reference/feature-status.md before treating this as current behavior.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from interlock.models import IdentityContext, PolicyDecision

logger = logging.getLogger(__name__)


class OPAClient:
    """Client for Open Policy Agent authorization decisions."""

    def __init__(
        self,
        opa_url: str = "http://localhost:8181",
        policy_path: str = "v1/data/onyx/authz",
        timeout_seconds: float = 1.0,
        fallback_to_builtin: bool = True,
    ) -> None:
        self._url = opa_url.rstrip("/")
        self._policy_path = policy_path.lstrip("/")
        self._timeout = timeout_seconds
        self._fallback = fallback_to_builtin
        self._available = False
        self._client: httpx.AsyncClient | None = None

    async def initialize(self) -> None:
        """Check OPA availability. Sets _available=False if unreachable."""
        self._client = httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await self._client.get(f"{self._url}/health")
            if resp.status_code == 200:
                self._available = True
                logger.info("OPAClient connected to %s", self._url)
            else:
                self._available = False
                logger.warning("OPA health check returned status %d", resp.status_code)
        except Exception:
            self._available = False
            logger.warning("OPA not reachable at %s - disabled", self._url)

    async def evaluate(
        self,
        identity: IdentityContext,
        source_id: str,
        operation: str,
        tables: list[str] | None = None,
        columns: list[str] | None = None,
    ) -> PolicyDecision | None:
        """Send authorization request to OPA and return a PolicyDecision.

        POST to {opa_url}/{policy_path} with the request context as input.

        Returns None (signaling caller should fall back to built-in engine)
        when OPA is unavailable, errors out, or times out - provided
        fallback_to_builtin is True. If fallback is disabled, raises on error.
        """
        if not self._available or self._client is None:
            if self._fallback:
                return None
            raise RuntimeError("OPA is not available and fallback is disabled")

        input_doc: dict[str, Any] = {
            "identity": {
                "identity_id": identity.identity_id,
                "user": identity.user,
                "agent_type": identity.agent_type.value,
                "team": identity.team,
                "roles": identity.roles,
            },
            "source_id": source_id,
            "operation": operation,
        }
        if tables is not None:
            input_doc["tables"] = tables
        if columns is not None:
            input_doc["columns"] = columns

        url = f"{self._url}/{self._policy_path}"

        try:
            resp = await self._client.post(url, json={"input": input_doc})
            resp.raise_for_status()
            body = resp.json()

            result = body.get("result", {})
            allowed = bool(result.get("allow", False))
            reason = result.get("reason", "")
            if not reason:
                reason = "OPA allowed" if allowed else "OPA denied"

            return PolicyDecision(
                allowed=allowed,
                reason=reason,
            )
        except httpx.TimeoutException:
            logger.warning("OPA request timed out")
            if self._fallback:
                return None
            raise
        except Exception:
            logger.exception("OPA evaluation failed")
            if self._fallback:
                return None
            raise

    async def shutdown(self) -> None:
        """Close the httpx client."""
        if self._client:
            await self._client.aclose()
            self._client = None
        self._available = False

    @property
    def available(self) -> bool:
        return self._available
