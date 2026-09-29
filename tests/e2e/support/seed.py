"""Idempotent seed harness for the compose-backed E2E environment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

import asyncpg

from interlock.admin.auth import hash_password
from interlock.config import AuthConfig
from interlock.connections.activation import activatable
from interlock.connections.connectors import (
    CONNECTOR_DEFINITIONS,
    role_templates_for_connector,
    source_metadata_for_connector,
)
from interlock.core.auth import AuthManager
from interlock.security.api_keys import hash_api_key_for_storage
from tests.e2e.support.config import E2EConfig, load_e2e_config


async def seed_all(config: E2EConfig | None = None) -> None:
    """Seed both the InterLock control DB and the upstream source DB."""
    cfg = config or load_e2e_config()
    control = await asyncpg.connect(cfg.control_dsn)
    source = await asyncpg.connect(cfg.source_dsn)
    try:
        await _seed_source_db(source)
        _seed_object_store(cfg)
        _seed_enterprise_upstreams(cfg)
        await _seed_control_db(control, cfg)
        _reset_http_upstream(cfg)
    finally:
        await source.close()
        await control.close()


async def _seed_source_db(conn: asyncpg.Connection) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            ssn TEXT NOT NULL,
            note TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            total NUMERIC(12, 2) NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mutation_log (
            id BIGSERIAL PRIMARY KEY,
            action TEXT NOT NULL,
            details TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
    await conn.execute("""
        INSERT INTO customers (id, name, email, ssn, note)
        VALUES
            (1, 'Ada Lovelace', 'ada@example.com', '123-45-6789', 'cacheable alpha'),
            (2, 'Grace Hopper', 'grace@example.com', '987-65-4321', 'cacheable beta')
        ON CONFLICT (id) DO UPDATE
        SET name = EXCLUDED.name,
            email = EXCLUDED.email,
            ssn = EXCLUDED.ssn,
            note = EXCLUDED.note
        """)
    await conn.execute("""
        INSERT INTO orders (id, customer_id, total, status)
        VALUES
            (100, 1, 42.50, 'open'),
            (101, 2, 101.25, 'closed')
        ON CONFLICT (id) DO UPDATE
        SET customer_id = EXCLUDED.customer_id,
            total = EXCLUDED.total,
            status = EXCLUDED.status
        """)


async def _seed_control_db(conn: asyncpg.Connection, cfg: E2EConfig) -> None:
    await _activate_every_connector(conn)
    await _upsert_sources(conn, cfg)
    agent_id = await _upsert_identity(
        conn,
        name="e2e-agent",
        api_key=cfg.agent_api_key,
        agent_type="codex",
        team="e2e",
        source_roles=[
            {"source_id": cfg.source_id_pg, "role": "analyst"},
            {"source_id": cfg.source_id_mysql, "role": "writer"},
            {"source_id": cfg.source_id_http, "role": "owner"},
            {"source_id": cfg.source_id_s3, "role": "reader"},
            {"source_id": cfg.source_id_spaces, "role": "reader"},
            {"source_id": cfg.source_id_opensearch, "role": "query_reader"},
            {"source_id": cfg.source_id_qdrant, "role": "collection_reader"},
            {"source_id": cfg.source_id_salesforce, "role": "object_reader"},
            {"source_id": cfg.source_id_notion, "role": "workspace_reader"},
        ],
        mapped_pg_role=None,
        pg_username=cfg.pg_username,
        pg_password=cfg.pg_password,
    )
    denied_id = await _upsert_identity(
        conn,
        name="e2e-denied-agent",
        api_key=cfg.denied_api_key,
        agent_type="codex",
        team="e2e",
        source_roles=[
            {"source_id": cfg.source_id_pg, "role": "blocked"},
            {"source_id": cfg.source_id_mysql, "role": "blocked"},
            {"source_id": cfg.source_id_http, "role": "blocked"},
            {"source_id": cfg.source_id_s3, "role": "blocked"},
            {"source_id": cfg.source_id_spaces, "role": "blocked"},
            {"source_id": cfg.source_id_opensearch, "role": "blocked"},
            {"source_id": cfg.source_id_qdrant, "role": "blocked"},
            {"source_id": cfg.source_id_salesforce, "role": "blocked"},
            {"source_id": cfg.source_id_notion, "role": "blocked"},
        ],
        mapped_pg_role=None,
        pg_username=None,
        pg_password=None,
    )
    await _upsert_source_roles(conn, cfg, agent_id=agent_id, denied_id=denied_id)
    await _upsert_policies(conn, cfg)
    await _upsert_discovery(conn, cfg)
    await _upsert_ingestion_job(conn, cfg)
    await conn.execute(
        """
        INSERT INTO cache_policies (source_id, strategy_override, enabled, semantic_enabled)
        VALUES ($1, 'deterministic_first', TRUE, FALSE),
               ($2, 'deterministic_first', TRUE, FALSE),
               ($3, 'deterministic_first', TRUE, FALSE),
               ($4, 'bypass', TRUE, FALSE),
               ($5, 'bypass', TRUE, FALSE),
               ($6, 'bypass', TRUE, FALSE),
               ($7, 'bypass', TRUE, FALSE),
               ($8, 'bypass', TRUE, FALSE),
               ($9, 'bypass', TRUE, FALSE)
        ON CONFLICT (source_id) DO UPDATE
        SET strategy_override = EXCLUDED.strategy_override,
            enabled = EXCLUDED.enabled,
            semantic_enabled = EXCLUDED.semantic_enabled
        """,
        cfg.source_id_pg,
        cfg.source_id_mysql,
        cfg.source_id_http,
        cfg.source_id_s3,
        cfg.source_id_spaces,
        cfg.source_id_opensearch,
        cfg.source_id_qdrant,
        cfg.source_id_salesforce,
        cfg.source_id_notion,
    )
    await conn.execute(
        "SELECT pg_notify('onyx_config_changed', $1)",
        json.dumps(
            {
                "source_ids": [
                    cfg.source_id_pg,
                    cfg.source_id_mysql,
                    cfg.source_id_http,
                    cfg.source_id_s3,
                    cfg.source_id_spaces,
                    cfg.source_id_opensearch,
                    cfg.source_id_qdrant,
                    cfg.source_id_salesforce,
                    cfg.source_id_notion,
                ],
            }
        ),
    )
    await conn.execute(
        "SELECT pg_notify('onyx_policy_changed', $1)",
        json.dumps({"seed": "e2e", "identity_id": agent_id}),
    )


async def _activate_every_connector(conn: asyncpg.Connection) -> None:
    """The suite registers sources on every connector, so all start active here.

    A deployment starts with only the live-proven set (migration 018); tests
    that exercise deactivation turn one off and back on themselves.
    """
    keys = [key for key in CONNECTOR_DEFINITIONS if activatable(key)]
    await conn.execute(
        """
        INSERT INTO connector_activation (connector_key, active)
        SELECT unnest($1::text[]), TRUE
        ON CONFLICT (connector_key) DO UPDATE SET active = TRUE
        """,
        keys,
    )


async def _upsert_identity(
    conn: asyncpg.Connection,
    *,
    name: str,
    api_key: str,
    agent_type: str,
    team: str,
    source_roles: list[dict[str, str]],
    mapped_pg_role: str | None,
    pg_username: str | None,
    pg_password: str | None,
) -> int:
    key_hash, key_version = _stored_api_key_hash(api_key)
    roles = sorted(
        {
            role
            for grant in source_roles
            for role in (f"{grant['source_id']}:{grant['role']}", grant["role"])
        }
    )
    return int(
        await conn.fetchval(
            """
            INSERT INTO identities
                (name, api_key_hash, agent_type, team, roles, mapped_pg_role,
                 enabled, metadata, pg_username, pg_password_hash, auth_metadata,
                 api_key_hash_version)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE, $7::jsonb, $8, $9, $10::jsonb, $11)
            ON CONFLICT (api_key_hash) DO UPDATE
            SET name = EXCLUDED.name,
                agent_type = EXCLUDED.agent_type,
                team = EXCLUDED.team,
                roles = EXCLUDED.roles,
                mapped_pg_role = EXCLUDED.mapped_pg_role,
                enabled = TRUE,
                metadata = EXCLUDED.metadata,
                pg_username = EXCLUDED.pg_username,
                pg_password_hash = EXCLUDED.pg_password_hash,
                auth_metadata = EXCLUDED.auth_metadata,
                api_key_hash_version = EXCLUDED.api_key_hash_version,
                updated_at = NOW()
            RETURNING id
            """,
            name,
            key_hash,
            agent_type,
            team,
            roles,
            mapped_pg_role,
            json.dumps({"seed": "e2e", "source_roles": source_roles}),
            pg_username,
            hash_password(pg_password) if pg_password else None,
            json.dumps({"seed": "e2e"}),
            key_version,
        )
    )


def _stored_api_key_hash(api_key: str) -> tuple[str, str]:
    """How the gateway under test stores keys.

    The compose stack accepts legacy SHA-256 hashes. A production-mode gateway
    refuses them, so the cloud certification passes the cluster's pepper and
    the seed stores the HMAC form that gateway computes.
    """
    pepper = os.environ.get("E2E_API_KEY_PEPPER", "")
    if pepper:
        return hash_api_key_for_storage(
            api_key, AuthConfig(api_key_pepper=pepper, allow_legacy_sha256_keys=False)
        )
    return AuthManager.hash_api_key(api_key), "sha256-v1"


async def _upsert_sources(conn: asyncpg.Connection, cfg: E2EConfig) -> None:
    pg_config = {
        "allow_private_egress": True,
        "host": cfg.compose_source_pg_host,
        "port": 5432,
        "database": cfg.source_pg_database,
        "user": cfg.source_pg_user,
        "password": cfg.source_pg_password,
    }
    # A production-mode gateway serves a PostgreSQL source only over verified
    # TLS. The cloud certification sets the CA path as the gateway pods see it.
    source_ca = os.environ.get("E2E_SOURCE_PG_SSL_CA", "").strip()
    if source_ca:
        pg_config.update({"sslmode": "verify-full", "ssl_ca": source_ca})
    mysql_config = {
        "allow_private_egress": True,
        "host": cfg.compose_source_mysql_host,
        "port": 3306,
        "database": cfg.source_mysql_database,
        "user": cfg.source_mysql_user,
        "password": cfg.source_mysql_password,
    }
    http_config = {
        "allow_private_egress": True,
        "base_url": cfg.compose_http_upstream_url,
    }
    s3_config = {
        "allow_private_egress": True,
        "bucket": cfg.s3_bucket,
        "prefix": cfg.s3_prefix,
        "region_name": "us-east-1",
        "endpoint_url": cfg.compose_s3_url,
        "aws_access_key_id": cfg.s3_access_key,
        "aws_secret_access_key": cfg.s3_secret_key,
    }
    spaces_config = {
        "allow_private_egress": True,
        "bucket": cfg.spaces_bucket,
        "prefix": cfg.spaces_prefix,
        "region_name": "us-east-1",
        "endpoint_url": cfg.compose_s3_url,
        "aws_access_key_id": cfg.s3_access_key,
        "aws_secret_access_key": cfg.s3_secret_key,
    }
    opensearch_config = {
        "allow_private_egress": True,
        "base_url": cfg.compose_opensearch_url,
        "indices": "claims-2026",
        "max_docs_per_index": "10",
    }
    qdrant_config = {
        "allow_private_egress": True,
        "base_url": cfg.compose_qdrant_url,
        "collections": "claims",
        "max_points_per_collection": "10",
    }
    salesforce_config = {
        "allow_private_egress": True,
        "base_url": cfg.compose_enterprise_sources_url,
        "token": "e2e-token",
        "objects": "Account,Case",
        "max_records_per_object": "10",
    }
    notion_config = {
        "allow_private_egress": True,
        "base_url": cfg.compose_enterprise_sources_url,
        "token": "e2e-token",
        "workspace": "e2e",
        "max_results": "10",
    }
    await conn.execute(
        """
        INSERT INTO data_sources
            (source_id, name, source_type, connection_config, cache_strategy, enabled, metadata)
        VALUES
            ($1, 'E2E PostgreSQL Source', 'postgresql', $2::jsonb,
             'deterministic_first', TRUE, $3::jsonb),
            ($4, 'E2E MySQL Source', 'mysql', $5::jsonb,
             'deterministic_first', TRUE, $6::jsonb),
            ($7, 'E2E HTTP Source', 'http', $8::jsonb,
             'deterministic_first', TRUE, $9::jsonb),
            ($10, 'E2E S3 Source', 's3', $11::jsonb,
             'bypass', TRUE, $12::jsonb),
            ($13, 'E2E DigitalOcean Spaces Source', 'object_storage', $14::jsonb,
             'bypass', TRUE, $15::jsonb),
            ($16, 'E2E OpenSearch Source', 'search', $17::jsonb,
             'bypass', TRUE, $18::jsonb),
            ($19, 'E2E Qdrant Source', 'search', $20::jsonb,
             'bypass', TRUE, $21::jsonb),
            ($22, 'E2E Salesforce Source', 'saas', $23::jsonb,
             'bypass', TRUE, $24::jsonb),
            ($25, 'E2E Notion Source', 'saas', $26::jsonb,
             'bypass', TRUE, $27::jsonb)
        ON CONFLICT (source_id) DO UPDATE
        SET name = EXCLUDED.name,
            source_type = EXCLUDED.source_type,
            connection_config = EXCLUDED.connection_config,
            cache_strategy = EXCLUDED.cache_strategy,
            enabled = TRUE,
            metadata = EXCLUDED.metadata,
            updated_at = NOW()
        """,
        cfg.source_id_pg,
        json.dumps(pg_config),
        json.dumps(
            {
                "seed": "e2e",
                "source_roles_version": 1,
            }
        ),
        cfg.source_id_mysql,
        json.dumps(mysql_config),
        json.dumps(
            {
                "seed": "e2e",
                **source_metadata_for_connector("mysql"),
            }
        ),
        cfg.source_id_http,
        json.dumps(http_config),
        json.dumps(
            {
                "seed": "e2e",
                "source_roles_version": 1,
            }
        ),
        cfg.source_id_s3,
        json.dumps(s3_config),
        json.dumps(
            {
                "seed": "e2e",
                **source_metadata_for_connector("s3"),
            }
        ),
        cfg.source_id_spaces,
        json.dumps(spaces_config),
        json.dumps(
            {
                "seed": "e2e",
                **source_metadata_for_connector("digitalocean_spaces"),
            }
        ),
        cfg.source_id_opensearch,
        json.dumps(opensearch_config),
        json.dumps(
            {
                "seed": "e2e",
                **source_metadata_for_connector("opensearch"),
            }
        ),
        cfg.source_id_qdrant,
        json.dumps(qdrant_config),
        json.dumps(
            {
                "seed": "e2e",
                **source_metadata_for_connector("qdrant"),
            }
        ),
        cfg.source_id_salesforce,
        json.dumps(salesforce_config),
        json.dumps(
            {
                "seed": "e2e",
                **source_metadata_for_connector("salesforce"),
            }
        ),
        cfg.source_id_notion,
        json.dumps(notion_config),
        json.dumps(
            {
                "seed": "e2e",
                **source_metadata_for_connector("notion"),
            }
        ),
    )
    schema_rows = [
        (cfg.source_id_pg, "public", "id", "integer", "identifier"),
        (cfg.source_id_pg, "public", "name", "text", "free_text"),
        (cfg.source_id_pg, "public", "email", "text", "identifier"),
        (cfg.source_id_pg, "public", "ssn", "text", "identifier"),
        (cfg.source_id_pg, "public", "note", "text", "free_text"),
        (cfg.source_id_mysql, "public", "id", "integer", "identifier"),
        (cfg.source_id_mysql, "public", "name", "varchar", "free_text"),
        (cfg.source_id_mysql, "public", "email", "varchar", "identifier"),
        (cfg.source_id_mysql, "public", "ssn", "varchar", "identifier"),
        (cfg.source_id_mysql, "public", "note", "text", "free_text"),
    ]
    for source_id, schema, column_name, data_type, classification in schema_rows:
        await conn.execute(
            """
            INSERT INTO schema_catalog
                (source_id, table_schema, table_name, column_name, data_type,
                 classification, pii_scan_tier, metadata)
            VALUES ($1, $2, 'customers', $3, $4, $5, 'fast_only', $6::jsonb)
            ON CONFLICT (source_id, table_schema, table_name, column_name) DO UPDATE
            SET data_type = EXCLUDED.data_type,
                classification = EXCLUDED.classification,
                pii_scan_tier = EXCLUDED.pii_scan_tier,
                metadata = EXCLUDED.metadata
            """,
            source_id,
            schema,
            column_name,
            data_type,
            classification,
            json.dumps({"seed": "e2e"}),
        )


async def _upsert_source_roles(
    conn: asyncpg.Connection,
    cfg: E2EConfig,
    *,
    agent_id: int,
    denied_id: int,
) -> None:
    role_specs = {
        cfg.source_id_pg: ("postgresql", ["read", "analyst", "write", "owner", "blocked"]),
        cfg.source_id_mysql: (
            "mysql",
            ["read", "writer", "maintainer", "schema_owner", "blocked"],
        ),
        cfg.source_id_http: ("http", ["read", "writer", "owner", "blocked"]),
        cfg.source_id_s3: ("s3", ["browser", "reader", "writer", "maintainer", "blocked"]),
        cfg.source_id_spaces: (
            "digitalocean_spaces",
            ["browser", "reader", "writer", "maintainer", "blocked"],
        ),
        cfg.source_id_opensearch: (
            "opensearch",
            ["query_reader", "blocked"],
        ),
        cfg.source_id_qdrant: (
            "qdrant",
            ["collection_reader", "blocked"],
        ),
        cfg.source_id_salesforce: (
            "salesforce",
            ["object_reader", "analyst", "blocked"],
        ),
        cfg.source_id_notion: (
            "notion",
            ["workspace_reader", "database_reader", "page_reader", "blocked"],
        ),
    }
    role_ids: dict[tuple[str, str], int] = {}
    for source_id, (source_type, role_keys) in role_specs.items():
        for role_key in role_keys:
            role_id = await conn.fetchval(
                """
                INSERT INTO source_roles
                    (source_id, role_key, name, description, review_required, metadata)
                VALUES ($1, $2, initcap(replace($2, '_', ' ')),
                        'E2E seeded source role', FALSE, $3::jsonb)
                ON CONFLICT (source_id, role_key) DO UPDATE
                SET name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    enabled = TRUE,
                    review_required = FALSE,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                RETURNING id
                """,
                source_id,
                role_key,
                json.dumps({"seed": "e2e"}),
            )
            if source_type in {
                "mysql",
                "s3",
                "digitalocean_spaces",
                "opensearch",
                "qdrant",
                "salesforce",
                "notion",
            }:
                await _replace_source_role_permissions(
                    conn,
                    int(role_id),
                    role_templates_for_connector(source_type).get(role_key, []),
                )
            else:
                await conn.execute(
                    "SELECT agentgate_seed_source_role_permissions($1, $2, $3)",
                    role_id,
                    role_key,
                    source_type,
                )
            role_ids[(source_id, role_key)] = int(role_id)

    grants = [
        (agent_id, cfg.source_id_pg, role_ids[(cfg.source_id_pg, "analyst")]),
        (agent_id, cfg.source_id_mysql, role_ids[(cfg.source_id_mysql, "writer")]),
        (agent_id, cfg.source_id_http, role_ids[(cfg.source_id_http, "owner")]),
        (agent_id, cfg.source_id_s3, role_ids[(cfg.source_id_s3, "reader")]),
        (agent_id, cfg.source_id_spaces, role_ids[(cfg.source_id_spaces, "reader")]),
        (agent_id, cfg.source_id_opensearch, role_ids[(cfg.source_id_opensearch, "query_reader")]),
        (agent_id, cfg.source_id_qdrant, role_ids[(cfg.source_id_qdrant, "collection_reader")]),
        (agent_id, cfg.source_id_salesforce, role_ids[(cfg.source_id_salesforce, "object_reader")]),
        (agent_id, cfg.source_id_notion, role_ids[(cfg.source_id_notion, "workspace_reader")]),
        (denied_id, cfg.source_id_pg, role_ids[(cfg.source_id_pg, "blocked")]),
        (denied_id, cfg.source_id_mysql, role_ids[(cfg.source_id_mysql, "blocked")]),
        (denied_id, cfg.source_id_http, role_ids[(cfg.source_id_http, "blocked")]),
        (denied_id, cfg.source_id_s3, role_ids[(cfg.source_id_s3, "blocked")]),
        (denied_id, cfg.source_id_spaces, role_ids[(cfg.source_id_spaces, "blocked")]),
        (denied_id, cfg.source_id_opensearch, role_ids[(cfg.source_id_opensearch, "blocked")]),
        (denied_id, cfg.source_id_qdrant, role_ids[(cfg.source_id_qdrant, "blocked")]),
        (denied_id, cfg.source_id_salesforce, role_ids[(cfg.source_id_salesforce, "blocked")]),
        (denied_id, cfg.source_id_notion, role_ids[(cfg.source_id_notion, "blocked")]),
    ]
    await conn.execute(
        """
        UPDATE identity_source_role_grants
        SET enabled = FALSE,
            updated_at = NOW(),
            metadata = COALESCE(metadata, '{}'::jsonb)
                       || '{"disabled_by_seed": true}'::jsonb
        WHERE identity_id = ANY($1::bigint[])
          AND COALESCE(metadata->>'seed', '') = 'e2e'
        """,
        [agent_id, denied_id],
    )
    for identity_id, source_id, role_id in grants:
        await conn.execute(
            """
            INSERT INTO identity_source_role_grants
                (identity_id, source_id, role_id, metadata)
            VALUES ($1, $2, $3, $4::jsonb)
            ON CONFLICT (identity_id, source_id, role_id) DO UPDATE
            SET enabled = TRUE,
                metadata = EXCLUDED.metadata,
                updated_at = NOW()
            """,
            identity_id,
            source_id,
            role_id,
            json.dumps({"seed": "e2e"}),
        )


async def _upsert_policies(conn: asyncpg.Connection, cfg: E2EConfig) -> None:
    await conn.execute("DELETE FROM policy_rules WHERE name LIKE 'e2e-%'")
    rules = [
        (
            "e2e-deny-blocked",
            100,
            {
                "source_ids": [
                    cfg.source_id_pg,
                    cfg.source_id_mysql,
                    cfg.source_id_http,
                    cfg.source_id_s3,
                    cfg.source_id_spaces,
                    cfg.source_id_opensearch,
                    cfg.source_id_qdrant,
                    cfg.source_id_salesforce,
                    cfg.source_id_notion,
                ],
                "roles": ["blocked"],
            },
            {"effect": "deny"},
        ),
        (
            "e2e-allow-agent",
            10,
            {
                "source_ids": [
                    cfg.source_id_pg,
                    cfg.source_id_mysql,
                    cfg.source_id_http,
                    cfg.source_id_s3,
                    cfg.source_id_spaces,
                    cfg.source_id_opensearch,
                    cfg.source_id_qdrant,
                    cfg.source_id_salesforce,
                    cfg.source_id_notion,
                ],
                "roles": [
                    "analyst",
                    "writer",
                    "owner",
                    "reader",
                    "query_reader",
                    "collection_reader",
                    "object_reader",
                    "workspace_reader",
                ],
                "operation_types": ["read", "write", "discovery"],
            },
            {"effect": "allow", "redact_columns": ["ssn", "email"]},
        ),
    ]
    for name, priority, conditions, actions in rules:
        await conn.execute(
            """
            INSERT INTO policy_rules (name, priority, conditions, actions, enabled)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, TRUE)
            """,
            name,
            priority,
            json.dumps(conditions),
            json.dumps(actions),
        )


async def _upsert_discovery(conn: asyncpg.Connection, cfg: E2EConfig) -> None:
    await conn.execute("""
        INSERT INTO category_taxonomy (path, name, description, document_count)
        VALUES ('engineering.runbooks'::ltree, 'Runbooks', 'E2E runbooks', 1)
        ON CONFLICT (path) DO UPDATE
        SET name = EXCLUDED.name,
            description = EXCLUDED.description,
            document_count = EXCLUDED.document_count
        """)
    asset_id = await conn.fetchval(
        """
        INSERT INTO discovery_assets
            (source_id, asset_type, asset_path, title, summary, category_path,
             topics, search_vector, quality_score, metadata)
        VALUES
            ($1, 'document', '/e2e/fixtures/discovery/runbook.md',
             'InterLock E2E Governance Runbook',
             $2::jsonb,
             'engineering.runbooks'::ltree,
             ARRAY['interlock','e2e','runbook'],
             to_tsvector('english',
                 'InterLock E2E governance runbook PostgreSQL HTTP approval audit redaction cache source routing'
             ),
             0.95,
             $3::jsonb)
        ON CONFLICT (source_id, asset_type, asset_path) DO UPDATE
        SET title = EXCLUDED.title,
            summary = EXCLUDED.summary,
            category_path = EXCLUDED.category_path,
            topics = EXCLUDED.topics,
            search_vector = EXCLUDED.search_vector,
            quality_score = EXCLUDED.quality_score,
            metadata = EXCLUDED.metadata,
            updated_at = NOW()
        RETURNING id
        """,
        cfg.source_id_pg,
        json.dumps(
            {
                "title": "InterLock E2E Governance Runbook",
                "doc_type": "markdown",
                "summary": (
                    "Seeded engineering runbook for InterLock E2E discovery tests "
                    "covering PostgreSQL routing, HTTP proxy governance, approvals, "
                    "redaction, cache invalidation, and audit correlation."
                ),
                "topics": ["interlock", "e2e", "runbook"],
                "category_path": "engineering.runbooks",
                "entities": [{"text": "InterLock", "type": "PRODUCT"}],
                "confidence": 1.0,
            }
        ),
        json.dumps({"seed": "e2e", "audience": "engineering"}),
    )
    await conn.execute(
        """
        INSERT INTO entity_document_xref
            (entity_text, entity_type, document_id, prominence, prominence_label,
             mention_count, context_snippet, metadata)
        VALUES
            ('InterLock', 'PRODUCT', $1, 1.0, 'primary', 8,
             'InterLock E2E Governance Runbook describes source routing, approvals, redaction, cache, and audit behavior.',
             $2::jsonb)
        ON CONFLICT (entity_text, entity_type, document_id) DO UPDATE
        SET prominence = EXCLUDED.prominence,
            prominence_label = EXCLUDED.prominence_label,
            mention_count = EXCLUDED.mention_count,
            context_snippet = EXCLUDED.context_snippet,
            metadata = EXCLUDED.metadata,
            updated_at = NOW()
        """,
        asset_id,
        json.dumps({"seed": "e2e"}),
    )


async def _upsert_ingestion_job(conn: asyncpg.Connection, cfg: E2EConfig) -> None:
    fixture = Path("/e2e/fixtures/discovery/runbook.md")
    await conn.execute(
        """
        INSERT INTO ingestion_jobs
            (source_id, file_path, status, stage, priority_score,
             progress_current, progress_total, metadata)
        VALUES ($1, $2, 'queued', 'queued', 10.0, 0, 1, $3::jsonb)
        ON CONFLICT (source_id, file_path) DO UPDATE
        SET status = 'queued',
            stage = 'queued',
            priority_score = EXCLUDED.priority_score,
            worker_id = NULL,
            lease_expires_at = NULL,
            progress_current = 0,
            progress_total = 1,
            error_message = NULL,
            metadata = EXCLUDED.metadata,
            updated_at = NOW()
        """,
        cfg.source_id_pg,
        str(fixture),
        json.dumps({"seed": "e2e"}),
    )


async def _replace_source_role_permissions(
    conn: asyncpg.Connection,
    role_id: int,
    permissions: list[dict[str, object]],
) -> None:
    await conn.execute("DELETE FROM source_role_permissions WHERE role_id = $1", role_id)
    for permission in permissions:
        await conn.execute(
            """
            INSERT INTO source_role_permissions
                (role_id, effect, action, resource_type, resource_pattern, constraints)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            role_id,
            permission.get("effect", "allow"),
            permission.get("action", "*"),
            permission.get("resource_type", "*"),
            permission.get("resource_pattern", "*"),
            json.dumps(permission.get("constraints") or {}),
        )


def _reset_http_upstream(cfg: E2EConfig) -> None:
    try:
        with urllib.request.urlopen(f"{cfg.http_upstream_url}/reset", timeout=5):
            return
    except Exception:
        return


def _seed_object_store(cfg: E2EConfig) -> None:
    """Put the discovery fixtures in the object store.

    This used to be a MinIO client container in the compose file. MinIO's images
    left public distribution, and the S3 upstream that replaced it creates its
    buckets at startup but holds no objects, so the fixtures are uploaded here -
    which also means a remote S3 endpoint can be seeded the same way. Buckets
    are created if missing so this does not depend on the server's own startup
    configuration.

    Degrades like the other upstream seeding: a stack without an object store
    still seeds everything else, and the S3 tests are the ones that fail.
    """
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "discovery" / "runbook.md"
    objects = (
        (cfg.s3_bucket, "discovery/runbook.md"),
        (cfg.s3_bucket, "runbook.md"),
        (cfg.spaces_bucket, "org/runbook.md"),
    )
    try:
        from tests.e2e.support.effects import _s3_client

        client = _s3_client(cfg)
        body = fixture.read_bytes()
        for bucket in {cfg.s3_bucket, cfg.spaces_bucket}:
            try:
                client.create_bucket(Bucket=bucket)
            except Exception:  # noqa: BLE001 - already present is the normal case
                pass
        for bucket, key in objects:
            client.put_object(Bucket=bucket, Key=key, Body=body)
    except Exception as exc:  # noqa: BLE001 - an absent object store is not fatal here
        print(f"object-store seeding skipped: {exc}", file=sys.stderr)


def _seed_enterprise_upstreams(cfg: E2EConfig) -> None:
    _json_request(
        "PUT",
        f"{cfg.opensearch_url}/claims-2026",
        {
            "mappings": {
                "properties": {
                    "title": {"type": "text"},
                    "body": {"type": "text"},
                    "updated_at": {"type": "date"},
                }
            }
        },
    )
    _json_request(
        "PUT",
        f"{cfg.opensearch_url}/claims-2026/_doc/doc-1?refresh=true",
        {
            "title": "Claims search certification",
            "body": "OpenSearch indexed document for InterLock E2E certification.",
            "updated_at": "2026-05-01T00:00:00Z",
        },
    )
    _json_request(
        "PUT",
        f"{cfg.qdrant_url}/collections/claims",
        {"vectors": {"size": 4, "distance": "Cosine"}},
    )
    _json_request(
        "PUT",
        f"{cfg.qdrant_url}/collections/claims/points?wait=true",
        {
            "points": [
                {
                    "id": 42,
                    "vector": [0.1, 0.2, 0.3, 0.4],
                    "payload": {
                        "title": "Claims vector certification",
                        "body": "Qdrant point for InterLock E2E certification.",
                    },
                }
            ]
        },
    )
    _reset_enterprise_sources(cfg)


def _reset_enterprise_sources(cfg: E2EConfig) -> None:
    try:
        with urllib.request.urlopen(f"{cfg.enterprise_sources_url}/reset", timeout=5):
            return
    except Exception:
        return


def _json_request(method: str, url: str, payload: dict[str, object]) -> None:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return
    except Exception:
        return


def seed_hashes_for_debug(config: E2EConfig | None = None) -> dict[str, str]:
    cfg = config or load_e2e_config()
    return {
        "agent_api_key_hash": hashlib.sha256(cfg.agent_api_key.encode()).hexdigest(),
        "denied_api_key_hash": hashlib.sha256(cfg.denied_api_key.encode()).hexdigest(),
    }


def main() -> None:
    asyncio.run(seed_all())


if __name__ == "__main__":
    main()
