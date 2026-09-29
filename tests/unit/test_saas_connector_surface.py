from __future__ import annotations

import importlib.util

from interlock.connections.connectors import (
    CONNECTOR_DEFINITIONS,
    get_adapter,
    role_templates_for_connector,
)


def test_legacy_parallel_saas_adapter_module_is_not_present() -> None:
    assert importlib.util.find_spec("interlock.connections.adapters.saas") is None


def test_saas_connectors_use_canonical_connector_registry() -> None:
    for key in ("salesforce", "notion", "zendesk", "slack", "github", "gitlab"):
        definition = CONNECTOR_DEFINITIONS[key]
        adapter = get_adapter(key, {"connector_key": key})

        assert adapter.definition is definition
        for method_name in (
            "probe",
            "introspect",
            "build_permission_request",
            "execute_read",
            "execute_write",
            "list_assets",
            "fetch_asset",
            "sync_incremental",
            "sanitize_config_for_admin",
        ):
            assert callable(getattr(adapter, method_name))


def test_saas_role_templates_are_defined_only_on_registry_definitions() -> None:
    for key in ("salesforce", "notion", "zendesk", "slack", "github", "gitlab"):
        templates = role_templates_for_connector(key)

        assert templates
        assert templates == CONNECTOR_DEFINITIONS[key].role_templates


def test_read_discovery_saas_connectors_do_not_publish_write_capability() -> None:
    for key in ("salesforce", "notion", "zendesk", "slack", "github", "gitlab"):
        definition = CONNECTOR_DEFINITIONS[key]

        assert definition.capabilities.supports_discovery is True
        assert definition.capabilities.supports_ingestion is True
        assert definition.capabilities.supports_write is False
