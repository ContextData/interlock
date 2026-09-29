"""Tests for interlock.config - defaults, YAML loading, and env var overrides."""

from __future__ import annotations

import os
import textwrap
import warnings
from pathlib import Path

import pytest

from interlock.config import InterLockConfig, apply_legacy_env_prefix, load_config


class TestDefaults:
    """InterLockConfig should be constructable with zero arguments and sensible defaults."""

    def test_default_creation(self) -> None:
        cfg = InterLockConfig()
        assert cfg.gateway.http_port == 3000
        assert cfg.gateway.pg_port == 5432
        assert cfg.gateway.mcp_port == 8001
        assert cfg.admin.port == 9090
        assert cfg.database.database == "onyx"
        assert cfg.auth.enabled is True
        assert cfg.cache.default_strategy == "lru"
        assert cfg.semantic_cache.default_auto_serve_threshold == 0.98
        assert cfg.semantic_cache.default_verify_threshold == 0.92
        assert cfg.semantic_cache.llm_timeout_ms == 500
        assert cfg.observability.enabled is True
        assert cfg.observability.otlp_endpoint is None
        assert cfg.observability.instrument_asgi is True
        assert cfg.gateway.pg_max_message_bytes == 16 * 1024 * 1024
        assert cfg.gateway.mcp_max_body_bytes == 1024 * 1024
        assert cfg.gateway.http_max_response_body_bytes == 50 * 1024 * 1024
        assert cfg.audit.durability_mode == "retriable"
        assert cfg.cache.strict_write_barrier is True
        assert cfg.observability.public_stats_enabled is False

    def test_pii_free_text_defaults(self) -> None:
        cfg = InterLockConfig()
        expected = [
            "description",
            "notes",
            "comment",
            "bio",
            "message",
            "body",
            "content",
            "remarks",
            "summary",
            "text",
        ]
        assert cfg.pii.free_text_column_patterns == expected

    def test_notifications_optional(self) -> None:
        cfg = InterLockConfig()
        assert cfg.notifications.slack_webhook_url is None


class TestYamlLoading:
    """Config should load values from a YAML file."""

    def test_load_from_yaml(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            gateway:
              http_port: 9090
              pg_port: 5555
            database:
              host: "db.example.com"
              max_pool: 50
            auth:
              session_ttl_seconds: 7200
        """)
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml_content)

        cfg = load_config(config_file)

        assert cfg.gateway.http_port == 9090
        assert cfg.gateway.pg_port == 5555
        # Defaults still apply for unset fields
        assert cfg.gateway.mcp_port == 8001
        assert cfg.database.host == "db.example.com"
        assert cfg.database.max_pool == 50
        assert cfg.auth.session_ttl_seconds == 7200

    def test_load_missing_yaml_uses_defaults(self, tmp_path: Path) -> None:
        cfg = load_config(tmp_path / "nonexistent.yaml")
        assert cfg.gateway.http_port == 3000


class TestEnvVarOverride:
    """Environment variables prefixed with INTERLOCK_ should override YAML and defaults."""

    def test_env_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INTERLOCK_GATEWAY__HTTP_PORT", "7777")
        monkeypatch.setenv("INTERLOCK_DATABASE__HOST", "envhost")
        monkeypatch.setenv("INTERLOCK_OBSERVABILITY__OTLP_ENDPOINT", "http://collector:4318")
        cfg = InterLockConfig()
        assert cfg.gateway.http_port == 7777
        assert cfg.database.host == "envhost"
        assert cfg.observability.otlp_endpoint == "http://collector:4318"

    def test_env_overrides_yaml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        yaml_content = textwrap.dedent("""\
            gateway:
              http_port: 9090
        """)
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml_content)

        monkeypatch.setenv("INTERLOCK_GATEWAY__HTTP_PORT", "6666")
        cfg = load_config(config_file)
        # Env var wins over YAML
        assert cfg.gateway.http_port == 6666


class TestLegacyEnvPrefixCompatibility:
    """ONYX_* must keep working for the documented compatibility window.

    The project was renamed from Onyx to InterLock. Deployments that still
    export the old prefix must not silently lose configuration, but a
    half-migrated environment must resolve to the new name rather than the
    stale one.
    """

    def test_legacy_variable_is_mapped_to_new_prefix(self) -> None:
        env = {"ONYX_ADMIN__PORT": "1234"}

        applied = apply_legacy_env_prefix(env)

        assert env["INTERLOCK_ADMIN__PORT"] == "1234"
        assert applied == ["ONYX_ADMIN__PORT"]

    def test_legacy_variable_emits_deprecation_warning(self) -> None:
        env = {"ONYX_DATABASE__HOST": "db.internal"}

        with pytest.warns(DeprecationWarning, match="INTERLOCK_DATABASE__HOST"):
            apply_legacy_env_prefix(env)

    def test_new_prefix_wins_when_both_are_set(self) -> None:
        env = {"ONYX_ADMIN__PORT": "1234", "INTERLOCK_ADMIN__PORT": "9999"}

        with pytest.warns(DeprecationWarning, match="ignored"):
            applied = apply_legacy_env_prefix(env)

        assert env["INTERLOCK_ADMIN__PORT"] == "9999"
        assert applied == []

    def test_no_legacy_variables_is_a_silent_noop(self) -> None:
        env = {"PATH": "/usr/bin", "INTERLOCK_ADMIN__PORT": "9090"}

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            applied = apply_legacy_env_prefix(env)

        assert applied == []
        assert caught == []

    def test_unrelated_variables_are_untouched(self) -> None:
        env = {"ONYXIAN": "not-a-prefix-match", "PATH": "/usr/bin"}

        apply_legacy_env_prefix(env)

        assert "INTERLOCKIAN" not in env
        assert env == {"ONYXIAN": "not-a-prefix-match", "PATH": "/usr/bin"}

    def test_legacy_config_path_is_honored(self, tmp_path: Path) -> None:
        config_file = tmp_path / "legacy.yaml"
        config_file.write_text("admin:\n  port: 4321\n")

        prior = os.environ.pop("INTERLOCK_CONFIG_PATH", None)
        os.environ["ONYX_CONFIG_PATH"] = str(config_file)
        try:
            assert load_config().admin.port == 4321
        finally:
            os.environ.pop("ONYX_CONFIG_PATH", None)
            if prior is not None:
                os.environ["INTERLOCK_CONFIG_PATH"] = prior
