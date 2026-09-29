"""Deep PII scanner gateway wiring regression tests."""

from __future__ import annotations

import inspect

from interlock.gateway import app as gateway_app


def test_gateway_lifespan_constructs_deep_pii_scanner_when_enabled() -> None:
    src = inspect.getsource(gateway_app.lifespan)

    assert "config.pii.deep_enabled" in src
    assert "PIIDeepScanner(max_workers=config.pii.deep_max_workers)" in src
    assert "await pii_deep_scanner.initialize()" in src
    assert "app.state.pii_deep_scanner" in src


def test_gateway_lifespan_passes_deep_scanner_and_config_to_response_processor() -> None:
    src = inspect.getsource(gateway_app.lifespan)

    assert "ResponseProcessor(" in src
    assert "deep_scanner=pii_deep_scanner" in src
    assert "config=config.pii" in src


def test_gateway_lifespan_shuts_down_deep_pii_scanner() -> None:
    src = inspect.getsource(gateway_app.lifespan)

    assert "await pii_deep_scanner.shutdown()" in src
