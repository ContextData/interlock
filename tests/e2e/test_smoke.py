"""E2E smoke test - asserts the compose stack is up and healthy.

This is the minimal gate that proves the deployment shape works. It does
not exercise application semantics; deeper tests live in
``test_protocol_paths.py``, ``test_governance.py``, etc.
"""

from __future__ import annotations

import urllib.request

import pytest


@pytest.mark.e2e
def test_gateway_health_ok(gateway_url: str) -> None:
    with urllib.request.urlopen(f"{gateway_url}/health", timeout=5) as resp:
        assert resp.status == 200


@pytest.mark.e2e
def test_admin_health_ok(admin_url: str) -> None:
    with urllib.request.urlopen(f"{admin_url}/health", timeout=5) as resp:
        assert resp.status == 200


@pytest.mark.e2e
def test_admin_unauth_dashboard_blocked(admin_url: str) -> None:
    """Phase 1 P0-F regression: dashboard must require auth."""
    req = urllib.request.Request(f"{admin_url}/dashboard/overview")
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        # Either 401 or a 302 redirect to /auth/login is acceptable.
        # If we get 200 here, the admin auth fix has regressed.
        assert resp.status in (302, 401), f"Admin dashboard should require auth, got {resp.status}"
    except urllib.error.HTTPError as exc:
        assert exc.code in (401, 403), f"Unexpected status {exc.code}"
