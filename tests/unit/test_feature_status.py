from __future__ import annotations

from interlock.feature_status import FEATURE_STATUSES, feature_status_map, public_beta_features


def test_feature_status_registry_has_unique_keys() -> None:
    keys = [feature.key for feature in FEATURE_STATUSES]

    assert len(keys) == len(set(keys))


def test_public_beta_features_are_not_planned_or_disabled() -> None:
    assert public_beta_features()

    for feature in public_beta_features():
        assert feature.state in {"certified", "beta"}
        assert feature.evidence


def test_facade_features_are_not_public_beta_enabled() -> None:
    statuses = feature_status_map()

    for key in (
        "semantic_cache",
        "category_classifier_enrichment",
        "qdrant_vector_backend",
        "alerts_notifications",
    ):
        feature = statuses[key]
        assert feature.public_beta is False
        assert feature.state in {"disabled", "planned"}
        assert feature.limitation


def test_otel_export_is_beta_with_runtime_wiring_evidence() -> None:
    feature = feature_status_map()["otel_export"]

    assert feature.state == "beta"
    assert feature.public_beta is True
    assert "Gateway" in feature.evidence
    assert "Admin" in feature.evidence
    assert "Worker" in feature.evidence
    assert "Final Boss" in feature.limitation


def test_deep_pii_scanner_is_beta_with_fail_closed_evidence() -> None:
    feature = feature_status_map()["deep_pii_scanner"]

    assert feature.state == "beta"
    assert feature.public_beta is True
    assert "PIIDeepScanner" in feature.evidence
    assert "fail-closed" in feature.evidence
    assert "PostgreSQL wire redaction remains fast-tier only" in feature.limitation


def test_rrf_ranking_is_beta_with_deterministic_evidence() -> None:
    feature = feature_status_map()["rrf_ranking"]

    assert feature.state == "beta"
    assert feature.public_beta is True
    assert "reciprocal rank fusion" in feature.evidence
    assert "raw-score independence" in feature.evidence
    assert "Category classifier scoping" in feature.limitation


def test_dependency_tracked_cache_invalidation_is_beta_with_runtime_evidence() -> None:
    feature = feature_status_map()["dependency_tracked_cache_invalidation"]

    assert feature.state == "beta"
    assert feature.public_beta is True
    assert "PG" in feature.evidence
    assert "HTTP" in feature.evidence
    assert "MCP" in feature.evidence
    assert "Redis invalidation events" in feature.evidence
    assert "Semantic cache serving remains disabled" in feature.limitation


def test_beta_connectors_have_explicit_write_limitation() -> None:
    feature = feature_status_map()["beta_enterprise_connectors"]

    assert feature.state == "beta"
    assert "read/discovery beta" in feature.limitation
