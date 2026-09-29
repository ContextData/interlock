"""One stored source must mean the same thing on every protocol.

InterLock opens upstream PostgreSQL connections from two places: `pg_proxy`
for the PG-wire path, and `ConnectionManager` for the MCP and HTTP paths. They
read the same `connection_config` row and used to disagree about it.

`pg_proxy` resolved secret references and accepted `ssl` or `sslmode`.
`ConnectionManager` read `connection_config["password"]` and
`connection_config["ssl"]` literally. So a source configured the way the admin
console recommends - `password_ref: env://...` - connected with **no
password** over MCP, and one configured `sslmode: require` connected with **no
encryption**, silently, depending only on which protocol the agent used.

Both now go through `read_connection_field`. These tests pin that they agree,
because the failure was invisible: each path worked in isolation.
"""

from __future__ import annotations

import inspect
import ssl
from typing import Any

import pytest

from interlock.connections.connectors import CONNECTOR_DEFINITIONS
from interlock.connections.manager import ConnectionManager, _ssl_argument
from interlock.connections.source_config import (
    CONFIG_ALIASES,
    SourceConfigValidationError,
    read_connection_field,
    resolve_config_value,
    upstream_tls_refusal,
    validate_source_config,
)


class TestSharedReader:
    def test_a_secret_reference_is_resolved(self, monkeypatch) -> None:
        """The half that silently produced password-less connections."""
        monkeypatch.setenv("TEST_PG_PASSWORD", "resolved-secret")

        value = read_connection_field({"password_ref": "env://TEST_PG_PASSWORD"}, "password")

        assert value == "resolved-secret"

    def test_a_reference_wins_over_a_stale_literal(self, monkeypatch) -> None:
        """Adding a `_ref` must take effect without clearing the old value first.

        Otherwise migrating a source to a secret reference silently keeps
        using the inline credential, which is the opposite of the intent.
        """
        monkeypatch.setenv("TEST_PG_PASSWORD", "from-reference")

        value = read_connection_field(
            {"password": "stale-inline", "password_ref": "env://TEST_PG_PASSWORD"}, "password"
        )

        assert value == "from-reference"

    def test_a_literal_is_passed_through_the_resolver_too(self, monkeypatch) -> None:
        """So a plain field may itself hold a reference."""
        monkeypatch.setenv("TEST_PG_PASSWORD", "resolved")

        assert read_connection_field({"password": "env://TEST_PG_PASSWORD"}, "password") == (
            "resolved"
        )

    @pytest.mark.parametrize(
        ("field", "config", "expected"),
        [
            ("ssl", {"sslmode": "require"}, "require"),
            ("ssl", {"ssl": "require"}, "require"),
            ("user", {"username": "bob"}, "bob"),
            ("user", {"user": "bob"}, "bob"),
            ("database", {"dbname": "app"}, "app"),
            ("ssl_verify", {"verify_ssl": "false"}, "false"),
        ],
    )
    def test_every_accepted_spelling_reaches_the_same_field(
        self, field: str, config: dict, expected: str
    ) -> None:
        assert read_connection_field(config, field) == expected

    def test_an_absent_field_is_none_rather_than_empty(self) -> None:
        """Empty string and absent must stay distinguishable.

        `password=""` is a deliberate empty password; a missing password is a
        configuration error. Collapsing them hides the second.
        """
        assert read_connection_field({}, "password") is None
        assert read_connection_field({"password": ""}, "password") == ""


class TestBothPathsAgree:
    def test_pg_proxy_delegates_to_the_shared_reader(self) -> None:
        """Not two implementations that happen to match today."""
        from interlock.gateway import pg_proxy

        source = inspect.getsource(pg_proxy._resolve_config_value)
        assert "resolve_config_value" in source, (
            "pg_proxy._resolve_config_value no longer delegates to the shared "
            "reader; the two connection paths can drift apart again"
        )

    def test_connection_manager_uses_the_shared_reader(self) -> None:
        source = inspect.getsource(ConnectionManager.get_pool)
        assert "read_connection_field" in source, (
            "ConnectionManager.get_pool reads connection_config directly again. "
            "A raw .get ignores every `_ref` form and alias, which is how a "
            "source configured through the admin console produced a connection "
            "with no password."
        )
        assert 'connection_config.get("password")' not in source

    def test_the_probe_path_reads_what_the_real_path_reads(self) -> None:
        """A probe that accepts a config the real connection rejects is a trap.

        This is the shape of the earlier defect where Test Connection accepted
        a configuration that Save then refused.
        """
        source = inspect.getsource(ConnectionManager.probe_unsaved)
        assert "read_connection_field" in source
        assert 'cfg.get("password")' not in source


class TestTlsTranslation:
    @pytest.mark.parametrize("mode", ["disable", "false", "off", "0"])
    def test_disabling_tls_is_honoured(self, mode: str) -> None:
        assert _ssl_argument(mode, {}) is False

    def test_a_mode_is_passed_through_unchanged(self) -> None:
        assert _ssl_argument("require", {}) == "require"
        assert _ssl_argument("verify-full", {}) == "verify-full"

    def test_verification_is_downgraded_only_when_explicitly_disabled(self) -> None:
        """Operators disable verification for managed clusters whose CA is not
        distributed with the deployment. That has to be honoured - but only
        when asked for."""
        assert _ssl_argument("verify-full", {"verify_ssl": False}) == "require"
        assert _ssl_argument("verify-full", {}) == "verify-full"
        assert _ssl_argument("verify-full", {"verify_ssl": True}) == "verify-full"

    def test_a_weaker_mode_is_never_silently_strengthened_or_weakened(self) -> None:
        """`require` stays `require` regardless of the verify flag.

        Downgrading only applies to modes that actually verify; touching
        anything else would change behaviour nobody asked to change.
        """
        assert _ssl_argument("require", {"verify_ssl": False}) == "require"
        assert _ssl_argument("require", {"verify_ssl": True}) == "require"

    def test_an_unrecognised_mode_is_passed_through_not_guessed(self) -> None:
        """A typo must surface as a connection error, not a quieter connection."""
        assert _ssl_argument("requrie", {}) == "requrie"


def test_the_alias_table_covers_every_field_a_connection_needs() -> None:
    """Pins the vocabulary, so adding a field forces a decision about spellings."""
    assert set(CONFIG_ALIASES) == {
        "user",
        "password",
        "database",
        "host",
        "port",
        "ssl",
        "ssl_verify",
        "ssl_ca",
    }
    for field, aliases in CONFIG_ALIASES.items():
        assert (
            field in aliases or f"{field}_ref" in aliases
        ), f"{field} cannot be spelled as itself, which will surprise someone"


def test_resolve_config_value_tries_keys_in_order() -> None:
    assert resolve_config_value({"b": "second", "a": "first"}, "a", "b") == "first"
    assert resolve_config_value({"b": "second"}, "a", "b") == "second"
    assert resolve_config_value({}, "a", "b") is None


# A throwaway self-signed CA, certificate only - there is no private key for
# it anywhere, and it is a trust anchor rather than a credential. Embedded
# rather than generated so the test has no openssl dependency, and dated far
# out because `load_verify_locations` does not care about expiry but a
# puzzled reader would.
_SELF_SIGNED_CA = """\
-----BEGIN CERTIFICATE-----
MIIDGzCCAgOgAwIBAgIUZ9fuWHNnmPwPRqs+MFhLOzvDRkIwDQYJKoZIhvcNAQEL
BQAwHDEaMBgGA1UEAwwRaW50ZXJsb2NrLXRlc3QtY2EwIBcNMjYwOTExMTc0MzUz
WhgPMjEyNjA4MTgxNzQzNTNaMBwxGjAYBgNVBAMMEWludGVybG9jay10ZXN0LWNh
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAz7Wvdd0Zh1UEvZkZOtkU
k7qnXkyf3VtFhZPQvoMC2mg7ELHG0FLcMqZnVafXjstFcQ1JhwgnecQi2erTjWU7
K9xjdvXb4eVEGIQ7DdjW8duFnDI2s79wnZI9dq5/y2olg9bvG3yUVCfihPsiFa0X
KaWmIM/rvlTu0yFzrTEJJugxo1ZAA/90HmkM92izscTTUyDbHLuJGwtvL77trMnk
CcEASbXs/hDw5cHg3Wm0NUypAXZilfWF0lQgkfNeNWNOL9K6KTM3X5uM+ejuq2BK
i4xe7VbXLc9YrpbVMjqmL9wXBQzPmtE9sOhJVG2IECEYks8h0gUsiigHazAeFn2z
AQIDAQABo1MwUTAdBgNVHQ4EFgQUNip/izkal2d1rcgoQCroOxVVsEIwHwYDVR0j
BBgwFoAUNip/izkal2d1rcgoQCroOxVVsEIwDwYDVR0TAQH/BAUwAwEB/zANBgkq
hkiG9w0BAQsFAAOCAQEAt4mgXNKf6t7VielEX6L4+KLyDySCbxfXJeLaR6zdkUoX
WsZo8Kk7GEEQDUL0+W6Mr/BsvIBnzXSSlDX0jpq4En8YG7EmXz9c3+8HBB7gnvjc
+ezUnrlWkbTzEq12EZ2yvnrp4eHcBWzvBYaDYz6zCwsrFy7PY7qUklB/oMOGBouX
lqyBumT+YS4uYQlfXJ67QFiSIW8x7UmUiX3uCH/baKqi5NH0eX5CCHGt48gKbzLc
SCLBYg2I2jQz4h68miiwg9ZP7ABwFBqQJcv7cxFwH1+AEe7iAEEFwARaWB3YHe88
v2hPrZpxsq2ooG+J8nUB2SagxMJwQVPQIw2Y0m4vIw==
-----END CERTIFICATE-----
"""


class TestTheConfiguredCaIsActuallyUsed:
    """A CA the operator supplied must reach the TLS handshake.

    `_ssl_argument` handed asyncpg the bare mode string and never looked at
    `ssl_ca`, so asyncpg fell back to libpq's default trust store at
    `~/.postgresql/root.crt` - a path that does not exist in the published
    image, which runs as uid 1000 with an emptyDir home. Every MCP query
    against a `verify-full` source therefore failed with a root-certificate
    error surfaced to the agent as an opaque `tool execution failed`, while
    the PG-wire path built a real context from the same stored field and
    worked. One source, two protocols, two answers again.
    """

    def test_a_verifying_mode_with_a_ca_builds_a_context_that_trusts_it(
        self, tmp_path: Any
    ) -> None:
        ca = tmp_path / "ca.crt"
        ca.write_text(_SELF_SIGNED_CA)

        result = _ssl_argument("verify-full", {"ssl_ca": str(ca)})

        assert isinstance(result, ssl.SSLContext), "the CA was not passed to asyncpg"
        assert result.verify_mode == ssl.CERT_REQUIRED
        assert result.check_hostname is True
        subjects = {cert["subject"][0][0][1] for cert in result.get_ca_certs()}
        assert "interlock-test-ca" in subjects

    def test_verify_ca_builds_a_context_that_does_not_check_the_hostname(
        self, tmp_path: Any
    ) -> None:
        """`verify-ca` verifies the chain but not the name; that is its whole
        difference from `verify-full`, and collapsing them would silently
        strengthen a mode the operator chose deliberately."""
        ca = tmp_path / "ca.crt"
        ca.write_text(_SELF_SIGNED_CA)

        result = _ssl_argument("verify-ca", {"ssl_ca": str(ca)})

        assert isinstance(result, ssl.SSLContext)
        assert result.check_hostname is False
        assert result.verify_mode == ssl.CERT_REQUIRED

    def test_the_libpq_spelling_of_the_ca_is_honoured_too(self, tmp_path: Any) -> None:
        """`sslrootcert` is the name every PostgreSQL operator types."""
        ca = tmp_path / "ca.crt"
        ca.write_text(_SELF_SIGNED_CA)

        result = _ssl_argument("verify-full", {"sslrootcert": str(ca)})

        assert isinstance(result, ssl.SSLContext)

    def test_no_ca_still_passes_the_mode_through(self) -> None:
        """Unchanged: with no CA configured the system trust store is correct,
        and asyncpg handles the mode string itself."""
        assert _ssl_argument("verify-full", {}) == "verify-full"

    def test_an_explicit_opt_out_still_wins_over_a_configured_ca(self, tmp_path: Any) -> None:
        """The operator asked for encryption without verification; a CA lying
        around in the config must not quietly re-enable it."""
        ca = tmp_path / "ca.crt"
        ca.write_text(_SELF_SIGNED_CA)

        assert _ssl_argument("verify-full", {"ssl_ca": str(ca), "verify_ssl": False}) == "require"

    def test_an_unreadable_ca_fails_loudly_rather_than_connecting_unverified(
        self, tmp_path: Any
    ) -> None:
        """Falling back to the mode string here would reproduce the original
        bug: a configured-but-unusable CA must not look like success."""
        with pytest.raises(FileNotFoundError):
            _ssl_argument("verify-full", {"ssl_ca": str(tmp_path / "missing.crt")})


class TestEverySpellingTheConnectPathHonoursCanBeStored:
    """The validator and the reader must agree on the vocabulary.

    `read_connection_field` resolves every member of a `CONFIG_ALIASES` group
    identically at connect time, but the strict allowlist was a separate,
    hand-maintained tuple. So `sslrootcert` - the libpq parameter name, and
    what every PostgreSQL operator types - was honoured if you could get it
    stored, and `POST`/`PUT /api/data-sources` refused to store it. The less
    familiar synonym was accepted instead.
    """

    @pytest.mark.parametrize("group", sorted(CONFIG_ALIASES), ids=sorted(CONFIG_ALIASES))
    def test_every_alias_in_a_group_validates_when_its_sibling_does(self, group: str) -> None:
        """Walks the table, so adding a spelling cannot reintroduce the gap."""
        aliases = CONFIG_ALIASES[group]
        for alias in aliases:
            validate_source_config(
                # allow_private_egress keeps the host group from doing DNS;
                # this test is about the allowlist, not about egress.
                {alias: "value", "allow_private_egress": True},
                connector_key="postgresql",
                source_type="postgresql",
                allowed_fields=(group,),
                strict_unknown=True,
            )

    def test_the_libpq_ca_spelling_is_accepted_on_a_real_postgresql_source(self) -> None:
        """The exact call that answered 422 on the live deployment."""
        config = {
            "host": "db.example.com",
            "allow_private_egress": True,
            "port": 25060,
            "database": "app",
            "user": "reader",
            "password_ref": "env://PGPASSWORD",
            "sslmode": "verify-full",
            "sslrootcert": "/run/secrets/db-ca/ca.crt",
        }

        validate_source_config(
            config,
            connector_key="postgresql",
            source_type="postgresql",
            allowed_fields=CONNECTOR_DEFINITIONS["postgresql"].credential_fields,
            secret_fields=CONNECTOR_DEFINITIONS["postgresql"].secret_fields,
            strict_unknown=True,
        )

    def test_a_genuinely_unknown_field_is_still_rejected(self) -> None:
        """Widening the allowlist must not turn strict mode into a no-op."""
        with pytest.raises(SourceConfigValidationError, match="unsupported"):
            validate_source_config(
                {"sslrootcrt": "/typo.crt"},
                connector_key="postgresql",
                source_type="postgresql",
                allowed_fields=("ssl_ca",),
                strict_unknown=True,
            )


# Each row is one stored source, and the answer both protocols must give for
# it in production. The defect was that they disagreed: a source registered
# `sslmode: require` with `verify_ssl: false` served rows happily over MCP
# while every PG-wire connection to the same source was refused.
_TLS_POSTURES = [
    pytest.param({"sslmode": "verify-full"}, None, id="verify-full-permitted"),
    pytest.param({"sslmode": "verify-ca"}, None, id="verify-ca-permitted"),
    # `require` encrypts without verifying: asyncpg's require with no root.crt
    # sets CERT_NONE, while PG-wire verified the same source. Production now
    # refuses it, so every protocol verifies.
    pytest.param({"sslmode": "require"}, "required", id="require-refused"),
    pytest.param({"sslmode": "prefer"}, "required", id="prefer-refused"),
    pytest.param({"sslmode": "allow"}, "required", id="allow-refused"),
    pytest.param({}, "required", id="no-tls-configured-refused"),
    pytest.param({"sslmode": "disable"}, "required", id="tls-disabled-refused"),
    pytest.param({"ssl": "off"}, "required", id="tls-off-refused"),
    pytest.param(
        {"sslmode": "verify-full", "verify_ssl": False}, "unverified", id="unverified-refused"
    ),
    pytest.param(
        {"sslmode": "verify-full", "ssl_verify": "no"}, "unverified", id="unverified-alias-refused"
    ),
    pytest.param(
        {"sslmode": "verify-full", "verify_ssl": True}, None, id="explicitly-verified-permitted"
    ),
]


class TestUpstreamTlsIsJudgedTheSameOnEveryProtocol:
    """One source configuration, one answer.

    The production gate existed in exactly one line and was handed only to the
    PG-wire proxy, so the connector path - MCP and HTTP - had no equivalent
    check at all. An operator could satisfy themselves that a source worked,
    over MCP or with the admin's Test Connection button, while the same source
    was refused on PG-wire and unverified everywhere it did connect. The
    stricter control existed; it simply was not applied uniformly.
    """

    @pytest.mark.parametrize("config,expected", _TLS_POSTURES)
    def test_production_refuses_exactly_the_insecure_postures(
        self, config: dict, expected: str | None
    ) -> None:
        refusal = upstream_tls_refusal(config, allow_insecure_tls=False)

        if expected is None:
            assert refusal is None, f"{config} should be permitted, got {refusal!r}"
        else:
            assert refusal is not None, f"{config} should be refused"
            assert expected in refusal.lower()

    @pytest.mark.parametrize("config,expected", _TLS_POSTURES)
    def test_outside_production_nothing_is_refused(
        self, config: dict, expected: str | None
    ) -> None:
        """Development keeps working against a local server with no TLS."""
        del expected
        assert upstream_tls_refusal(config, allow_insecure_tls=True) is None

    def test_the_pg_wire_path_consults_the_shared_decision(self) -> None:
        from interlock.gateway import pg_proxy

        source = inspect.getsource(pg_proxy._open_upstream_connection)
        assert "upstream_tls_refusal" in source, (
            "the PG-wire path judges upstream TLS on its own again; that is "
            "how it came to disagree with the connector path"
        )

    def test_the_connector_path_consults_the_shared_decision(self) -> None:
        source = inspect.getsource(ConnectionManager.get_pool)
        assert "upstream_tls_refusal" in source, (
            "ConnectionManager no longer checks upstream TLS, so MCP and HTTP "
            "would accept a source PG-wire refuses"
        )

    def test_both_protocols_give_the_operator_the_same_wording(self) -> None:
        """A refusal that reads differently per protocol invites the guess that
        the protocols disagree - which, until this was shared, they did."""
        refusal = upstream_tls_refusal({"sslmode": "disable"}, allow_insecure_tls=False)

        assert refusal == "Verified upstream PostgreSQL TLS is required"
        assert (
            upstream_tls_refusal(
                {"sslmode": "verify-full", "verify_ssl": False}, allow_insecure_tls=False
            )
            == "Unverified upstream PostgreSQL TLS is disabled"
        )


class TestProductionWiringCannotBeDroppedSilently:
    """The gate defaults permissive, so losing the wiring reopens the hole.

    `ConnectionManager` defaults `allow_insecure_upstream_tls=True` so that a
    test or a script constructing one behaves as it always did. That makes the
    call sites load-bearing: if either app stopped passing the flag, every
    deployment would quietly go back to accepting unverified upstreams over
    MCP and HTTP, and nothing would fail.
    """

    @pytest.mark.parametrize(
        "module", ["interlock.gateway.app", "interlock.admin.app"], ids=["gateway", "admin"]
    )
    def test_both_apps_derive_the_gate_from_the_environment(self, module: str) -> None:
        import importlib

        source = inspect.getsource(importlib.import_module(module))

        assert "allow_insecure_upstream_tls=allows_insecure_upstream_tls(config)" in source, (
            f"{module} no longer tells ConnectionManager whether insecure "
            "upstream TLS is permitted; the default is permissive, so this "
            "silently stops enforcing in production"
        )

    def test_the_default_is_permissive_so_existing_callers_are_unaffected(self) -> None:
        signature = inspect.signature(ConnectionManager.__init__)

        assert signature.parameters["allow_insecure_upstream_tls"].default is True


class TestTestConnectionGivesTheSameAnswerAsTheRealConnection:
    """Test Connection must refuse what production refuses, before connecting.

    `probe_unsaved` never consulted the gate, so the console and API Test
    Connection buttons reported a configuration healthy that every query path
    would refuse the moment it was saved - verified live against a real managed
    database. It is also what the registered-source test route
    calls, so a stored insecure source looked healthy too.
    """

    @pytest.mark.parametrize("config,expected", _TLS_POSTURES)
    async def test_probe_and_gate_agree_for_every_posture(
        self, config: dict, expected: str | None
    ) -> None:
        from unittest.mock import AsyncMock, patch

        cfg = {"host": "db.example.com", "allow_private_egress": True, "port": 5432, **config}
        connection = AsyncMock()
        connect = AsyncMock(return_value=connection)
        with patch("interlock.connections.manager.asyncpg.connect", connect):
            status = await ConnectionManager.probe_unsaved(
                "postgresql", cfg, connector_key="postgresql", allow_insecure_tls=False
            )

        refusal = upstream_tls_refusal(cfg, allow_insecure_tls=False)
        if expected is None:
            assert status.healthy, status.error
        else:
            assert not status.healthy
            assert status.error == refusal
            connect.assert_not_called()  # refused before any network contact

    async def test_development_still_probes_an_insecure_config(self) -> None:
        from unittest.mock import AsyncMock, patch

        connect = AsyncMock(return_value=AsyncMock())
        with patch("interlock.connections.manager.asyncpg.connect", connect):
            status = await ConnectionManager.probe_unsaved(
                "postgresql",
                {"host": "db.example.com", "allow_private_egress": True, "sslmode": "disable"},
                connector_key="postgresql",
            )

        assert status.healthy, status.error
        connect.assert_awaited_once()

    def test_the_probe_consults_the_shared_decision(self) -> None:
        source = inspect.getsource(ConnectionManager.probe_unsaved)
        assert "upstream_tls_refusal" in source


class TestOneDefinitionOfTheTlsPolicy:
    def test_the_verifying_modes_live_in_one_place(self) -> None:
        from interlock.connections import manager, source_config

        assert manager.VERIFYING_TLS_MODES is source_config.VERIFYING_TLS_MODES
        assert frozenset({"verify-ca", "verify-full"}) == source_config.VERIFYING_TLS_MODES

    @pytest.mark.parametrize(
        "environment,allowed",
        [("production", False), ("development", True), ("test", True)],
    )
    def test_only_production_forbids_insecure_upstream_tls(
        self, environment: str, allowed: bool
    ) -> None:
        from interlock.config import InterLockConfig, allows_insecure_upstream_tls

        config = InterLockConfig.model_construct(environment=environment)
        assert allows_insecure_upstream_tls(config) is allowed
