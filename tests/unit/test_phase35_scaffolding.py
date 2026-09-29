"""Phase 3-5 scaffolding tests.

Covers the new RequestPipeline shell, OpenTelemetry no-op fallback,
and the secret resolver. These are foundations for Phases 4-5 of the
plan; richer behaviour lands later.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import interlock.observability.health as health_module
import interlock.observability.otel as otel_module
from interlock.observability.otel import (
    configure_otel,
    configure_service_otel,
    get_meter,
    get_tracer,
)
from interlock.pipeline.runtime import PipelineContext, RequestPipeline, make_stage
from interlock.secrets.resolver import MissingSecretError, resolve

# ---------------------------------------------------------------------------
# RequestPipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_runs_stages_in_order_and_records_latency() -> None:
    order: list[str] = []

    async def s1(ctx: PipelineContext) -> None:
        order.append("s1")
        ctx.audit_status = "success"

    async def s2(ctx: PipelineContext) -> None:
        order.append("s2")
        await asyncio.sleep(0)

    pipeline = RequestPipeline([make_stage("authn", s1), make_stage("policy", s2)])
    ctx = await pipeline.run(PipelineContext(protocol="pg"))

    assert order == ["s1", "s2"]
    assert "authn" in ctx.stage_latencies
    assert "policy" in ctx.stage_latencies
    assert ctx.stage_latencies["authn"] >= 0


@pytest.mark.asyncio
async def test_pipeline_records_latency_even_when_stage_raises() -> None:
    async def boom(ctx: PipelineContext) -> None:
        raise RuntimeError("boom")

    pipeline = RequestPipeline([make_stage("crash", boom)])
    with pytest.raises(RuntimeError):
        await pipeline.run(PipelineContext(protocol="mcp"))


# ---------------------------------------------------------------------------
# OpenTelemetry no-op
# ---------------------------------------------------------------------------


def test_otel_default_is_noop() -> None:
    tracer = get_tracer()
    meter = get_meter()
    # No-op tracer accepts span context manager.
    with tracer.start_as_current_span("x") as span:
        span.set_attribute("k", "v")
    counter = meter.create_counter("requests")
    counter.add(1)


def test_control_plane_health_metrics_are_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object, object]] = []

    class FakeInstrument:
        def __init__(self, name: str) -> None:
            self.name = name

        def add(self, value: object, attributes: object = None) -> None:
            calls.append((self.name, value, attributes))

        def record(self, value: object, attributes: object = None) -> None:
            calls.append((self.name, value, attributes))

    class FakeMeter:
        def create_counter(self, name: str, **_: object) -> FakeInstrument:
            return FakeInstrument(name)

        def create_histogram(self, name: str, **_: object) -> FakeInstrument:
            return FakeInstrument(name)

    monkeypatch.setattr(health_module, "_HEALTH_COUNTER", None)
    monkeypatch.setattr(health_module, "_HEALTH_DURATION", None)
    monkeypatch.setattr(health_module, "get_meter", lambda: FakeMeter())

    health_module.record_control_plane_health(
        service="gateway",
        route="/ready",
        status="ready",
        duration_ms=1.5,
    )

    assert calls[0][0] == "interlock.control_plane.health.checks"
    assert calls[0][1] == 1
    assert calls[0][2]["health.status"] == "ready"
    assert calls[1][0] == "interlock.control_plane.health.duration_ms"
    assert calls[1][1] == 1.5


def test_otel_configure_without_endpoint_stays_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert configure_otel(service_name="interlock-test") is False
    # Still works.
    with get_tracer().start_as_current_span("x"):
        pass


def test_otel_service_bootstrap_respects_disabled_config() -> None:
    config = SimpleNamespace(
        observability=SimpleNamespace(
            enabled=False,
            otlp_endpoint="http://collector:4318",
            instrument_asgi=True,
        )
    )

    result = configure_service_otel(service_name="interlock-test", config=config, app=object())

    assert result.configured is False
    assert result.instrumented is False
    assert result.endpoint == "http://collector:4318"
    assert result.reason == "disabled"


def test_otel_service_bootstrap_uses_configured_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def fake_configure_otel(*, service_name: str, otlp_endpoint: str | None = None) -> bool:
        calls["service_name"] = service_name
        calls["endpoint"] = otlp_endpoint
        return True

    def fake_instrument_asgi_app(app: object) -> bool:
        calls["app"] = app
        return True

    monkeypatch.setattr(otel_module, "configure_otel", fake_configure_otel)
    monkeypatch.setattr(otel_module, "instrument_asgi_app", fake_instrument_asgi_app)
    app = object()
    config = SimpleNamespace(
        observability=SimpleNamespace(
            enabled=True,
            otlp_endpoint="http://collector:4318",
            instrument_asgi=True,
        )
    )

    result = configure_service_otel(service_name="interlock-test", config=config, app=app)

    assert result.configured is True
    assert result.instrumented is True
    assert result.endpoint == "http://collector:4318"
    assert result.reason == "configured"
    assert calls == {
        "service_name": "interlock-test",
        "endpoint": "http://collector:4318",
        "app": app,
    }


def test_otel_service_bootstrap_reports_missing_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(otel_module, "configure_otel", lambda **_: False)
    config = SimpleNamespace(
        observability=SimpleNamespace(
            enabled=True,
            otlp_endpoint="http://collector:4318",
            instrument_asgi=True,
        )
    )

    result = configure_service_otel(service_name="interlock-test", config=config, app=object())

    assert result.configured is False
    assert result.instrumented is False
    assert result.endpoint == "http://collector:4318"
    assert result.reason == "sdk_unavailable"


# ---------------------------------------------------------------------------
# Secret resolver
# ---------------------------------------------------------------------------


def test_resolve_passes_through_plain_value() -> None:
    assert resolve("hello") == "hello"
    assert resolve(None) is None


def test_resolve_env_returns_environment_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERLOCK_TEST_SECRET", "shhh")
    assert resolve("env://INTERLOCK_TEST_SECRET") == "shhh"


def test_resolve_env_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEFINITELY_NOT_SET_42", raising=False)
    with pytest.raises(MissingSecretError):
        resolve("env://DEFINITELY_NOT_SET_42")


def test_resolve_file_reads_allowlisted_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "mounted-secret.txt"
    secret_file.write_text("super-secret\n", encoding="utf-8")
    monkeypatch.setenv("INTERLOCK_SECRET_FILE_ROOTS", str(tmp_path))

    assert resolve(f"file://{secret_file}") == "super-secret\n"


def test_resolve_file_rejects_paths_outside_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "allowed"
    denied_root = tmp_path / "denied"
    allowed_root.mkdir()
    denied_root.mkdir()
    secret_file = denied_root / "secret.txt"
    secret_file.write_text("nope", encoding="utf-8")
    monkeypatch.setenv("INTERLOCK_SECRET_FILE_ROOTS", str(allowed_root))

    with pytest.raises(MissingSecretError):
        resolve(f"file://{secret_file}")


def test_resolve_unknown_scheme_raises() -> None:
    with pytest.raises(MissingSecretError):
        resolve("nonexistent-scheme://foo")
