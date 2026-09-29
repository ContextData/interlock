"""Phase 0 P0-T01 tests: config schema validation.

These tests confirm that:
- Unknown top-level keys in ``config.yaml`` are rejected (so typos do not
  silently fall through to default values).
- Field-range constraints reject obvious bad values (negative TTLs, port
  out of range).
- YAML parse errors raise ``ConfigValidationError`` rather than being
  swallowed.
- Missing config files use defaults (current behavior preserved).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from interlock.config import load_config
from interlock.errors import ConfigValidationError


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


# ---------------------------------------------------------------------------
# Positive
# ---------------------------------------------------------------------------


def test_default_config_loads_when_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.gateway.http_port == 3000
    assert cfg.cache.l1_max_size > 0


def test_valid_yaml_overrides_defaults(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "gateway:\n  http_port: 4001\ncache:\n  l1_ttl_seconds: 30\n",
    )
    cfg = load_config(p)
    assert cfg.gateway.http_port == 4001
    assert cfg.cache.l1_ttl_seconds == 30


# ---------------------------------------------------------------------------
# Negative - unknown keys
# ---------------------------------------------------------------------------


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "garbage_section:\n  foo: 1\n")
    with pytest.raises(ConfigValidationError) as exc:
        load_config(p)
    assert "garbage_section" in str(exc.value)


def test_unknown_nested_key_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "gateway:\n  htttp_port: 9999\n")
    with pytest.raises(ConfigValidationError):
        load_config(p)


# ---------------------------------------------------------------------------
# Negative - field validation
# ---------------------------------------------------------------------------


def test_negative_ttl_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "cache:\n  l1_ttl_seconds: -5\n")
    with pytest.raises(ConfigValidationError):
        load_config(p)


def test_port_out_of_range_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "gateway:\n  http_port: 99999\n")
    with pytest.raises(ConfigValidationError):
        load_config(p)


def test_zero_workers_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "gateway:\n  http_workers: 0\n")
    with pytest.raises(ConfigValidationError):
        load_config(p)


# ---------------------------------------------------------------------------
# Negative - parse errors
# ---------------------------------------------------------------------------


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, "gateway:\n  http_port: : :\n")
    with pytest.raises(ConfigValidationError):
        load_config(p)


def test_yaml_top_level_must_be_mapping(tmp_path: Path) -> None:
    p = _write(tmp_path, "- 1\n- 2\n- 3\n")
    with pytest.raises(ConfigValidationError):
        load_config(p)


def test_threshold_out_of_range_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "semantic_cache:\n  default_auto_serve_threshold: 1.5\n")
    with pytest.raises(ConfigValidationError):
        load_config(p)


def test_empty_yaml_returns_defaults(tmp_path: Path) -> None:
    p = _write(tmp_path, "")
    cfg = load_config(p)
    assert cfg.gateway.http_port == 3000


def test_runtime_limit_ranges_are_enforced(tmp_path: Path) -> None:
    p = _write(tmp_path, "gateway:\n  pg_max_message_bytes: 4\n")
    with pytest.raises(ConfigValidationError):
        load_config(p)


def test_production_configuration_fails_closed_without_security_requirements(
    tmp_path: Path,
) -> None:
    p = _write(tmp_path, "environment: production\n")
    with pytest.raises(ConfigValidationError) as exc:
        load_config(p)
    message = str(exc.value)
    assert "admin.secret_key" in message
    assert "auth.api_key_pepper" in message
    assert "auth.allow_legacy_sha256_keys" in message
    assert "PostgreSQL client credentials" in message
    assert "database.ssl_mode" in message
    assert "audit.durability_mode" in message


def test_production_configuration_accepts_explicit_safe_settings(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
environment: production
admin:
  secret_key: '0123456789abcdef0123456789abcdef'
  cookie_secure: true
auth:
  api_key_pepper: 'abcdef0123456789abcdef0123456789'
  allow_legacy_sha256_keys: false
gateway:
  pg_trusted_tls_offload: true
database:
  ssl_mode: verify-full
  ssl_ca_file: /run/secrets/control-db-ca.pem
audit:
  durability_mode: strict
""",
    )
    cfg = load_config(p)
    assert cfg.environment == "production"


def test_every_configuration_field_is_described() -> None:
    """The configuration reference is generated from these descriptions.

    A field without one would appear in the published reference with nothing
    to say what it does, so adding a field means describing it.
    """
    from pydantic import BaseModel

    from interlock.config import InterLockConfig

    undescribed: list[str] = []

    def walk(model: type[BaseModel], prefix: str = "") -> None:
        for name, field in model.model_fields.items():
            if not (field.description or "").strip():
                undescribed.append(prefix + name)
            annotation = field.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                walk(annotation, prefix + name + ".")

    walk(InterLockConfig)
    assert not undescribed, f"describe these configuration fields: {undescribed}"
