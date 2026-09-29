"""asyncpg connection pool factory."""

from __future__ import annotations

import json
import ssl

import asyncpg

from interlock.config import DatabaseConfig
from interlock.errors import ConfigValidationError


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Register custom type codecs on each new connection."""
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pg_pool(config: DatabaseConfig) -> asyncpg.Pool:
    """Create an asyncpg connection pool from config.

    Sets statement_cache_size=0 for proxy compatibility (PgBouncer, etc.).
    Registers JSON/JSONB codecs so JSONB columns are returned as Python dicts.
    """
    pool: asyncpg.Pool = await asyncpg.create_pool(
        host=config.host,
        port=config.port,
        database=config.database,
        user=config.user,
        password=config.password,
        min_size=config.min_pool,
        max_size=config.max_pool,
        statement_cache_size=0,
        ssl=_build_ssl_context(config),
        init=_init_connection,
    )
    return pool


def _build_ssl_context(config: DatabaseConfig) -> ssl.SSLContext | None:
    if bool(config.ssl_cert_file) != bool(config.ssl_key_file):
        raise ConfigValidationError(
            "database.ssl_cert_file and database.ssl_key_file must be configured together"
        )

    if config.ssl_mode == "disable":
        if config.ssl_cert_file or config.ssl_key_file:
            raise ConfigValidationError(
                "database.ssl_cert_file and database.ssl_key_file require "
                "database.ssl_mode to be require, verify-ca, or verify-full"
            )
        return None

    if config.ssl_mode == "require":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    else:
        if not config.ssl_ca_file:
            raise ConfigValidationError(
                f"database.ssl_ca_file is required for ssl_mode={config.ssl_mode}"
            )
        context = ssl.create_default_context(cafile=config.ssl_ca_file)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = config.ssl_mode == "verify-full"

    if hasattr(ssl, "TLSVersion"):
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    if config.ssl_cert_file and config.ssl_key_file:
        context.load_cert_chain(config.ssl_cert_file, config.ssl_key_file)
    return context


async def close_pg_pool(pool: asyncpg.Pool) -> None:
    """Gracefully close a connection pool."""
    await pool.close()
