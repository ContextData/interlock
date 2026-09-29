"""Stable E2E configuration shared by seed scripts and pytest fixtures."""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass


@dataclass(frozen=True)
class E2EConfig:
    """Connection details and deterministic credentials for the E2E stack."""

    admin_url: str = "http://localhost:9090"
    gateway_url: str = "http://localhost:3001"
    http_upstream_url: str = "http://localhost:18088"
    enterprise_sources_url: str = "http://localhost:18090"
    s3_url: str = "http://localhost:19000"
    opensearch_url: str = "http://localhost:19200"
    qdrant_url: str = "http://localhost:16333"

    control_pg_host: str = "127.0.0.1"
    control_pg_port: int = 5433
    control_pg_user: str = "onyx"
    control_pg_password: str = "onyx_dev"
    control_pg_database: str = "onyx"

    source_pg_host: str = "127.0.0.1"
    source_pg_port: int = 55433
    source_pg_user: str = "source_user"
    source_pg_password: str = "source_pass"
    source_pg_database: str = "source_db"

    compose_source_pg_host: str = "source-postgres"
    source_mysql_host: str = "127.0.0.1"
    source_mysql_port: int = 33306
    source_mysql_user: str = "source_user"
    source_mysql_password: str = "source_pass"
    source_mysql_database: str = "source_db"
    compose_source_mysql_host: str = "source-mysql"
    compose_http_upstream_url: str = "http://http-upstream:8088"
    compose_enterprise_sources_url: str = "http://enterprise-sources:8090"
    compose_s3_url: str = "http://s3-upstream:9090"
    compose_opensearch_url: str = "http://source-opensearch:9200"
    compose_qdrant_url: str = "http://source-qdrant:6333"

    agent_api_key: str = "ag-e2e-api-key"
    denied_api_key: str = "ag-e2e-denied-key"
    pg_username: str = "e2e_pg_user"
    pg_password: str = "e2e-pg-password"
    source_id_pg: str = "e2e_pg"
    source_id_mysql: str = "e2e_mysql"
    source_id_http: str = "e2e_http"
    source_id_s3: str = "e2e_s3"
    source_id_spaces: str = "e2e_spaces"
    source_id_opensearch: str = "e2e_opensearch"
    source_id_qdrant: str = "e2e_qdrant"
    source_id_salesforce: str = "e2e_salesforce"
    source_id_notion: str = "e2e_notion"
    s3_bucket: str = "interlock-e2e"
    s3_prefix: str = "discovery/"
    spaces_bucket: str = "interlock-spaces-e2e"
    spaces_prefix: str = "org/"
    s3_access_key: str = "e2e-s3-access-key"
    s3_secret_key: str = "e2e-s3-secret-key"

    admin_username: str = "admin"
    admin_password: str = "e2e-admin-password"

    @property
    def control_dsn(self) -> str:
        return (
            f"postgresql://{self.control_pg_user}:{self.control_pg_password}"
            f"@{self.control_pg_host}:{self.control_pg_port}/{self.control_pg_database}"
        )

    @property
    def source_dsn(self) -> str:
        return (
            f"postgresql://{self.source_pg_user}:{self.source_pg_password}"
            f"@{self.source_pg_host}:{self.source_pg_port}/{self.source_pg_database}"
        )

    @property
    def gateway_pg_params_api_key(self) -> dict[str, object]:
        return {
            "host": os.environ.get("INTERLOCK_PG_HOST", "127.0.0.1"),
            "port": int(os.environ.get("INTERLOCK_PG_PORT", "5434")),
            "user": "e2e_api_key_login",
            "password": self.agent_api_key,
            "database": self.source_id_pg,
            **({"ssl": ssl_context} if (ssl_context := _gateway_pg_ssl()) else {}),
        }

    @property
    def gateway_pg_params_password(self) -> dict[str, object]:
        return {
            "host": os.environ.get("INTERLOCK_PG_HOST", "127.0.0.1"),
            "port": int(os.environ.get("INTERLOCK_PG_PORT", "5434")),
            "user": self.pg_username,
            "password": self.pg_password,
            "database": self.source_id_pg,
            **({"ssl": ssl_context} if (ssl_context := _gateway_pg_ssl()) else {}),
        }


def _gateway_pg_ssl() -> object | None:
    """TLS settings for the gateway's PostgreSQL listener, when required.

    The Helm chart sets `pg_require_client_tls: true` as a production
    invariant, but the compose stack leaves it at its `False` default, so the
    suite has historically only ever exercised the plaintext path. Setting
    `INTERLOCK_PG_SSL` lets the same tests run against a
    production-configured gateway; leaving it unset keeps compose behaviour
    unchanged.

    `INTERLOCK_PG_SSL=require` encrypts without verifying the server, which
    suits a disposable certification cluster using a throwaway CA. Point
    `INTERLOCK_PG_SSL_ROOT_CERT` at that CA to verify instead.
    """
    mode = os.environ.get("INTERLOCK_PG_SSL", "").strip()
    if not mode:
        return None

    root_cert = os.environ.get("INTERLOCK_PG_SSL_ROOT_CERT", "").strip()
    if root_cert:
        context = ssl.create_default_context(cafile=root_cert)
        return context

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def load_e2e_config() -> E2EConfig:
    """Load E2E config from env, falling back to deterministic local defaults."""
    return E2EConfig(
        admin_url=os.environ.get("ADMIN_URL", "http://localhost:9090"),
        gateway_url=os.environ.get("GATEWAY_URL", "http://localhost:3001"),
        http_upstream_url=os.environ.get("HTTP_UPSTREAM_URL", "http://localhost:18088"),
        enterprise_sources_url=os.environ.get("ENTERPRISE_SOURCES_URL", "http://localhost:18090"),
        s3_url=os.environ.get("E2E_S3_URL", "http://localhost:19000"),
        opensearch_url=os.environ.get("OPENSEARCH_URL", "http://localhost:19200"),
        qdrant_url=os.environ.get("QDRANT_URL", "http://localhost:16333"),
        control_pg_host=os.environ.get("E2E_CONTROL_PG_HOST", "127.0.0.1"),
        control_pg_port=int(os.environ.get("E2E_CONTROL_PG_PORT", "5433")),
        control_pg_user=os.environ.get("E2E_CONTROL_PG_USER", "onyx"),
        control_pg_password=os.environ.get("E2E_CONTROL_PG_PASSWORD", "onyx_dev"),
        control_pg_database=os.environ.get("E2E_CONTROL_PG_DATABASE", "onyx"),
        source_pg_host=os.environ.get("E2E_SOURCE_PG_HOST", "127.0.0.1"),
        source_pg_port=int(os.environ.get("E2E_SOURCE_PG_PORT", "55433")),
        source_pg_user=os.environ.get("E2E_SOURCE_PG_USER", "source_user"),
        source_pg_password=os.environ.get("E2E_SOURCE_PG_PASSWORD", "source_pass"),
        source_pg_database=os.environ.get("E2E_SOURCE_PG_DATABASE", "source_db"),
        compose_source_pg_host=os.environ.get("E2E_COMPOSE_SOURCE_PG_HOST", "source-postgres"),
        source_mysql_host=os.environ.get("E2E_SOURCE_MYSQL_HOST", "127.0.0.1"),
        source_mysql_port=int(os.environ.get("E2E_SOURCE_MYSQL_PORT", "33306")),
        source_mysql_user=os.environ.get("E2E_SOURCE_MYSQL_USER", "source_user"),
        source_mysql_password=os.environ.get("E2E_SOURCE_MYSQL_PASSWORD", "source_pass"),
        source_mysql_database=os.environ.get("E2E_SOURCE_MYSQL_DATABASE", "source_db"),
        compose_source_mysql_host=os.environ.get("E2E_COMPOSE_SOURCE_MYSQL_HOST", "source-mysql"),
        compose_http_upstream_url=os.environ.get(
            "E2E_COMPOSE_HTTP_UPSTREAM_URL", "http://http-upstream:8088"
        ),
        compose_enterprise_sources_url=os.environ.get(
            "E2E_COMPOSE_ENTERPRISE_SOURCES_URL", "http://enterprise-sources:8090"
        ),
        compose_s3_url=os.environ.get("E2E_COMPOSE_S3_URL", "http://s3-upstream:9090"),
        compose_opensearch_url=os.environ.get(
            "E2E_COMPOSE_OPENSEARCH_URL", "http://source-opensearch:9200"
        ),
        compose_qdrant_url=os.environ.get("E2E_COMPOSE_QDRANT_URL", "http://source-qdrant:6333"),
        agent_api_key=os.environ.get("E2E_AGENT_API_KEY", "ag-e2e-api-key"),
        denied_api_key=os.environ.get("E2E_DENIED_API_KEY", "ag-e2e-denied-key"),
        pg_username=os.environ.get("E2E_PG_USERNAME", "e2e_pg_user"),
        pg_password=os.environ.get("E2E_PG_PASSWORD", "e2e-pg-password"),
        source_id_pg=os.environ.get("E2E_SOURCE_ID_PG", "e2e_pg"),
        source_id_mysql=os.environ.get("E2E_SOURCE_ID_MYSQL", "e2e_mysql"),
        source_id_http=os.environ.get("E2E_SOURCE_ID_HTTP", "e2e_http"),
        source_id_s3=os.environ.get("E2E_SOURCE_ID_S3", "e2e_s3"),
        source_id_spaces=os.environ.get("E2E_SOURCE_ID_SPACES", "e2e_spaces"),
        source_id_opensearch=os.environ.get("E2E_SOURCE_ID_OPENSEARCH", "e2e_opensearch"),
        source_id_qdrant=os.environ.get("E2E_SOURCE_ID_QDRANT", "e2e_qdrant"),
        source_id_salesforce=os.environ.get("E2E_SOURCE_ID_SALESFORCE", "e2e_salesforce"),
        source_id_notion=os.environ.get("E2E_SOURCE_ID_NOTION", "e2e_notion"),
        s3_bucket=os.environ.get("E2E_S3_BUCKET", "interlock-e2e"),
        s3_prefix=os.environ.get("E2E_S3_PREFIX", "discovery/"),
        spaces_bucket=os.environ.get("E2E_SPACES_BUCKET", "interlock-spaces-e2e"),
        spaces_prefix=os.environ.get("E2E_SPACES_PREFIX", "org/"),
        s3_access_key=os.environ.get("E2E_S3_ACCESS_KEY", "e2e-s3-access-key"),
        s3_secret_key=os.environ.get("E2E_S3_SECRET_KEY", "e2e-s3-secret-key"),
        admin_username=os.environ.get("E2E_ADMIN_USERNAME", "admin"),
        admin_password=os.environ.get("E2E_ADMIN_PASSWORD", "e2e-admin-password"),
    )
