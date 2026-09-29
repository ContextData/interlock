"""The credential loader and, above all, the redactor that guards it.

These are unit tests with no `live` marker: they run in the default suite,
without credentials, against synthetic input. That is deliberate. The redactor
is the single component standing between a live certification run and a leaked
secret, so it must be covered by the tests everyone runs, not by the tests
only a credential holder runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.live.support.credentials import (
    Redactor,
    assert_no_secrets,
    environment_for,
    export_environment,
    load_credentials,
)

# Assembled from fragments so the file carries no scannable secret shape.
_FAKE_SLACK_TOKEN = "xoxb-" + "1111111111-2222222222-" + "AbCdEfGhIjKlMnOpQrSt"
_FAKE_PASSWORD = "hunter2superSecret"
_FAKE_AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"


class TestRedactor:
    def test_a_labelled_secret_is_redacted_to_its_end(self) -> None:
        """The regression this class exists for.

        The prior implementation wrote the character class as `[^\\\\s,;]` in a
        raw string, which is the set {backslash, s, comma, semicolon} rather
        than whitespace. Redaction therefore stopped at the first letter "s"
        in the value and emitted everything after it in clear. A redactor that
        reveals the tail of a token is worse than none, because the output
        looks redacted.
        """
        redactor = Redactor([])

        assert redactor.text(f"password={_FAKE_PASSWORD}") == "password=<redacted>"

        result = redactor.text(f"password={_FAKE_PASSWORD}")
        assert "uperSecret" not in result, "redaction stopped early and leaked the tail"

    def test_known_secret_values_are_replaced_anywhere_they_appear(self) -> None:
        redactor = Redactor([_FAKE_PASSWORD])

        result = redactor.text(f"connect failed for {_FAKE_PASSWORD} at host")

        assert _FAKE_PASSWORD not in result
        assert "<redacted>" in result

    def test_longer_secrets_are_replaced_before_shorter_substrings(self) -> None:
        """A secret containing another must not be partially revealed."""
        short, long = "abc123def", "abc123def456ghi"
        redactor = Redactor([short, long])

        result = redactor.text(f"value={long}")

        assert "456ghi" not in result, "the longer secret was split by the shorter one"

    def test_secret_shaped_tokens_are_redacted_without_being_known(self) -> None:
        """Defence for a credential the parser never captured."""
        redactor = Redactor([])

        assert _FAKE_SLACK_TOKEN not in redactor.text(f"auth {_FAKE_SLACK_TOKEN} used")
        assert _FAKE_AWS_KEY not in redactor.text(f"key {_FAKE_AWS_KEY} used")

    def test_a_private_key_body_is_redacted_whole(self) -> None:
        pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----"
        redactor = Redactor([])

        assert "MIIEvQIBADANBgkq" not in redactor.text(pem)

    def test_a_managed_database_hostname_is_redacted(self) -> None:
        """Not a credential, but this project treats it as disclosure."""
        redactor = Redactor([])
        host = "somecluster-do-user-1234567-0.h.db.ondigitalocean.com"  # secret-scan: allow - synthetic, asserts the redactor catches this shape

        assert host not in redactor.text(f"connected to {host}")

    def test_obj_blanks_secretish_keys_at_any_depth(self) -> None:
        redactor = Redactor([])

        result = redactor.obj({"outer": {"password": _FAKE_PASSWORD, "host": "db.example.com"}})

        assert result == {"outer": {"password": "<redacted>", "host": "db.example.com"}}

    def test_very_short_values_are_not_treated_as_secrets(self) -> None:
        """Otherwise a password of "1" redacts every digit in the report."""
        redactor = Redactor(["1", "ok"])

        assert redactor.text("row 1 is ok") == "row 1 is ok"


class TestAssertNoSecrets:
    def test_it_raises_when_a_secret_survives(self) -> None:
        with pytest.raises(AssertionError, match="survived redaction"):
            assert_no_secrets(f"report body {_FAKE_PASSWORD}", [_FAKE_PASSWORD])

    def test_the_failure_message_does_not_contain_the_secret(self) -> None:
        """A leak check that leaks into its own traceback is not a check."""
        with pytest.raises(AssertionError) as excinfo:
            assert_no_secrets(f"body {_FAKE_PASSWORD}", [_FAKE_PASSWORD])

        assert _FAKE_PASSWORD not in str(excinfo.value)
        assert f"{len(_FAKE_PASSWORD)} chars" in str(excinfo.value)

    def test_it_passes_on_redacted_text(self) -> None:
        assert_no_secrets("body <redacted>", [_FAKE_PASSWORD])

    def test_it_catches_a_sensitive_shape_that_is_not_a_declared_secret(self) -> None:
        """The half a literal-value check cannot see.

        Regression: a certification report was written containing the managed
        database's hostname. Every declared secret had been redacted, so the
        literal check passed - but `secret_scan.py` blocks that hostname shape
        as infrastructure disclosure. A check that is true and incomplete is
        how a green result stops meaning anything.
        """
        host = "somecluster-do-user-1234567-0.h.db.ondigitalocean.com"  # secret-scan: allow - synthetic, asserts the redactor catches this shape

        with pytest.raises(AssertionError, match="do-user"):
            assert_no_secrets(f"host: {host}", [])

    def test_the_shape_failure_message_does_not_repeat_the_value(self) -> None:
        host = "somecluster-do-user-1234567-0.h.db.ondigitalocean.com"  # secret-scan: allow - synthetic, asserts the redactor catches this shape

        with pytest.raises(AssertionError) as excinfo:
            assert_no_secrets(f"host: {host}", [])

        assert host not in str(excinfo.value)


_SAMPLE = """\
# S3
{aws_key}
abcdefghijklmnopqrstuvwxyz0123456789ABCD

Bucket: interlock-live
Region: us-east-1
Prefix: cert/

# postgres
host: pg.example.com
port: 5432
db_name: appdb
users:
- generic_read_user (password: readpass123)
- generic_write_user (password: writepass123)

# mysql
host: mysql.example.com
port: 3306
db_name: appdb
users:
- generic_read_user (password: myreadpass123)

# Slack
Channel Name: #interlock-cert
Channel ID: C0123456789
Bot User OAuth Token: {slack_token}

## Bot Token Scopes
channels: read

# Google Workspace
username: cert@example.com
password: ignored-entirely
""".format(slack_token=_FAKE_SLACK_TOKEN, aws_key=_FAKE_AWS_KEY)


@pytest.fixture
def parsed(tmp_path: Path):
    path = tmp_path / ".env.live"
    path.write_text(_SAMPLE)
    return load_credentials(path, gws_credentials_file=tmp_path / "absent.json")


class TestParser:
    def test_every_section_is_recognised(self, parsed) -> None:
        assert parsed.postgres["host"] == "pg.example.com"
        assert parsed.mysql["host"] == "mysql.example.com"
        assert parsed.s3["bucket"] == "interlock-live"
        assert parsed.slack["channel_id"] == "C0123456789"
        assert parsed.google_workspace["subject_user"] == "cert@example.com"

    def test_database_users_and_passwords_are_paired(self, parsed) -> None:
        assert parsed.postgres["users"] == {
            "generic_read_user": "readpass123",
            "generic_write_user": "writepass123",
        }

    def test_s3_positional_keys_are_read_in_order(self, parsed) -> None:
        assert parsed.s3["aws_access_key_id"] == _FAKE_AWS_KEY
        assert parsed.s3["aws_secret_access_key"].startswith("abcdef")

    def test_the_slack_channel_name_loses_its_hash(self, parsed) -> None:
        assert parsed.slack["channel_name"] == "interlock-cert"

    def test_an_unrecognised_header_ends_the_previous_section(self, parsed) -> None:
        """`## Bot Token Scopes` must not append scope lines to Slack config."""
        assert "channels" not in parsed.slack

    def test_the_workspace_domain_is_derived_from_the_subject_user(self, parsed) -> None:
        assert parsed.google_workspace["workspace_domain"] == "example.com"

    def test_the_google_password_is_never_captured(self, parsed) -> None:
        """Google rejects password auth; capturing it would only risk leaking it."""
        assert "password" not in parsed.google_workspace
        assert "ignored-entirely" not in parsed.secrets

    def test_every_password_reaches_the_secrets_list(self, parsed) -> None:
        """The redactor is only as complete as this list."""
        for expected in ("readpass123", "writepass123", "myreadpass123", _FAKE_SLACK_TOKEN):
            assert expected in parsed.secrets

    def test_a_missing_file_yields_empty_credentials_not_an_error(self, tmp_path: Path) -> None:
        """A contributor without credentials must run the suite unchanged."""
        creds = load_credentials(tmp_path / "nope", gws_credentials_file=tmp_path / "nope.json")

        assert creds.secrets == []
        assert not creds.has_postgres()
        assert not creds.has_slack()


class TestServiceAccountLoading:
    def _write(self, path: Path, payload: dict) -> Path:
        import json

        path.write_text(json.dumps(payload))
        return path

    def test_a_valid_service_account_key_is_loaded(self, tmp_path: Path) -> None:
        key = self._write(
            tmp_path / "sa.json",
            {
                "type": "service_account",
                "private_key": "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----",
                "client_email": "svc@proj.iam.gserviceaccount.com",
                "client_id": "1234567890",
                "project_id": "proj",
            },
        )
        (tmp_path / ".env.live").write_text("# Google Workspace\nusername: cert@example.com\n")

        creds = load_credentials(tmp_path / ".env.live", gws_credentials_file=key)

        assert creds.has_google_workspace()
        assert creds.google_workspace["client_id"] == "1234567890"

    def test_an_oauth_client_config_is_rejected_with_a_clear_message(self, tmp_path: Path) -> None:
        """The most common wrong file, caught at load rather than by Google."""
        key = self._write(tmp_path / "sa.json", {"type": "authorized_user", "client_id": "x"})

        with pytest.raises(ValueError, match="service account key is required"):
            load_credentials(tmp_path / "absent", gws_credentials_file=key)

    def test_a_truncated_key_names_the_missing_fields(self, tmp_path: Path) -> None:
        key = self._write(tmp_path / "sa.json", {"type": "service_account", "client_id": "x"})

        with pytest.raises(ValueError, match="private_key"):
            load_credentials(tmp_path / "absent", gws_credentials_file=key)


class TestEnvironmentExport:
    def test_passwords_are_exported_per_user(self, parsed) -> None:
        env = environment_for(parsed)

        assert env["INTERLOCK_LIVE_PG_PASSWORD_GENERIC_READ_USER"] == "readpass123"
        assert env["INTERLOCK_LIVE_PG_PASSWORD_GENERIC_WRITE_USER"] == "writepass123"

    def test_an_already_set_variable_wins(self, parsed, monkeypatch) -> None:
        """This is what makes one code path serve both a local file and CI."""
        monkeypatch.setenv("INTERLOCK_LIVE_PG_HOST", "from-ci.example.com")

        export_environment(parsed)

        import os

        assert os.environ["INTERLOCK_LIVE_PG_HOST"] == "from-ci.example.com"

    def test_export_returns_names_never_values(self, parsed, monkeypatch) -> None:
        for key in list(environment_for(parsed)):
            monkeypatch.delenv(key, raising=False)

        exported = export_environment(parsed)

        assert "INTERLOCK_LIVE_SLACK_BOT_TOKEN" in exported
        for name in exported:
            assert _FAKE_SLACK_TOKEN not in name
            assert "readpass123" not in name
