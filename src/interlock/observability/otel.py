"""OpenTelemetry scaffolding.

The actual ``opentelemetry-sdk`` dependency is optional - if it is not
installed (default for unit-test installs), this module exposes a
no-op tracer and meter that record nothing. Production deployments
install the optional ``otel`` extra and the gateway/admin/worker
entrypoints call ``configure_otel()`` to wire OTLP export.

This keeps the surface small in Phase 5 without forcing every developer
to install the full OTel stack just to run ``pytest``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from interlock import release_version

logger = logging.getLogger(__name__)


class _NoopSpan:
    def set_attribute(self, key: str, value: object) -> None:
        return None

    def set_status(self, status: object, description: str | None = None) -> None:
        return None

    def record_exception(self, exc: BaseException) -> None:
        return None

    def end(self) -> None:
        return None


class _NoopTracer:
    @contextmanager
    def start_as_current_span(self, name: str, **_: object) -> Iterator[_NoopSpan]:
        yield _NoopSpan()


class _NoopMeter:
    def create_counter(self, *_, **__):
        return _NoopInstrument()

    def create_histogram(self, *_, **__):
        return _NoopInstrument()

    def create_up_down_counter(self, *_, **__):
        return _NoopInstrument()


class _NoopInstrument:
    def add(self, *_, **__):
        return None

    def record(self, *_, **__):
        return None


_TRACER: object = _NoopTracer()
_METER: object = _NoopMeter()


@dataclass(frozen=True)
class OTelBootstrapResult:
    """Result of service-level OpenTelemetry bootstrap."""

    configured: bool
    instrumented: bool
    endpoint: str | None
    reason: str


def get_tracer() -> object:
    """Return the active tracer (or a no-op if OTel is not installed)."""
    return _TRACER


def get_meter() -> object:
    return _METER


def configure_otel(
    *,
    service_name: str,
    otlp_endpoint: str | None = None,
) -> bool:
    """Wire OTLP export when the SDK is installed.

    Falls back to no-op silently when ``opentelemetry-sdk`` is missing,
    so the unit-test environment is unaffected.
    """
    global _TRACER, _METER
    endpoint = otlp_endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        logger.info("OTel: no OTLP endpoint configured; using no-op tracer")
        return False

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OTel SDK not installed; install interlock-runtime[otel] to enable export. "
            "Tracer/meter remain no-op."
        )
        return False

    resource = Resource.create({"service.name": service_name})
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(tracer_provider)
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint))],
    )
    metrics.set_meter_provider(meter_provider)

    _TRACER = trace.get_tracer("interlock", release_version())
    _METER = metrics.get_meter("interlock", release_version())
    logger.info("OTel configured: endpoint=%s service=%s", endpoint, service_name)
    return True


def instrument_asgi_app(app: Any) -> bool:
    """Install ASGI request instrumentation when the optional package exists."""
    try:
        from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
    except ImportError:
        logger.warning(
            "OTel ASGI instrumentation not installed; install interlock-runtime[otel] to "
            "trace HTTP request spans."
        )
        return False

    state = getattr(app, "state", None)
    if state is not None and getattr(state, "otel_instrumented", False):
        return True

    app.add_middleware(OpenTelemetryMiddleware)
    if state is not None:
        state.otel_instrumented = True
    return True


def configure_service_otel(
    *,
    service_name: str,
    config: Any | None = None,
    app: Any | None = None,
) -> OTelBootstrapResult:
    """Configure OTLP export and optional ASGI instrumentation for a service."""
    observability = getattr(config, "observability", None)
    enabled = bool(getattr(observability, "enabled", True))
    endpoint = getattr(observability, "otlp_endpoint", None) or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    instrument_asgi = bool(getattr(observability, "instrument_asgi", True))

    if not enabled:
        return OTelBootstrapResult(
            configured=False,
            instrumented=False,
            endpoint=endpoint,
            reason="disabled",
        )

    if not endpoint:
        configure_otel(service_name=service_name, otlp_endpoint=None)
        return OTelBootstrapResult(
            configured=False,
            instrumented=False,
            endpoint=None,
            reason="no_endpoint",
        )

    configured = configure_otel(service_name=service_name, otlp_endpoint=endpoint)
    if not configured:
        return OTelBootstrapResult(
            configured=False,
            instrumented=False,
            endpoint=endpoint,
            reason="sdk_unavailable",
        )

    instrumented = False
    if app is not None and instrument_asgi:
        instrumented = instrument_asgi_app(app)

    return OTelBootstrapResult(
        configured=True,
        instrumented=instrumented,
        endpoint=endpoint,
        reason="configured",
    )
