"""Configuration for live certification, and the run stamp everything derives from.

`LiveConfig` describes the five real upstream systems. It deliberately does
*not* describe InterLock itself: the gateway, admin and control-plane
coordinates come from `load_e2e_config()`, because a live run drives the same
locally-running stack that the e2e suite does. Only the upstreams differ.

Every value here is read from the environment, which
`tests.live.support.credentials.export_environment` populates at conftest
import time. Nothing reads the credential file directly except that loader.

`RUN_ID` is computed once, at import, and every artifact a live run creates
derives its name from it. That is what makes a run identifiable after the
fact: if a process is killed before teardown, `make live-sweep` can find its
leftovers by prefix, and a later run can never collide with them.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime

from tests.e2e.support.config import E2EConfig, load_e2e_config

# One stamp per process, referenced by tests, teardown and the report so all
# three agree on what "this run" means. Lowercase alphanumeric only: it is
# interpolated into SQL identifiers, S3 keys and Slack message text.
RUN_ID = os.environ.get("INTERLOCK_LIVE_RUN_ID") or (
    f"lc{datetime.now(UTC):%Y%m%d%H%M%S}{secrets.token_hex(3)}"
)

# Shared by every system so the sweeper has one prefix to look for.
ARTIFACT_PREFIX = "interlock_live_cert"
ARTIFACT_PREFIX_DASHED = "interlock-live-cert"

# Control-plane object names. Deliberately not stamped with RUN_ID: these are
# configuration rather than artifacts, they are upserted, and teardown removes
# them by this fixed prefix. A stamped source id would leak a new row per run.
SOURCE_ID_PREFIX = "live_cert_"
AGENT_IDENTITY = "live-cert-agent"
BLOCKED_IDENTITY = "live-cert-blocked"
POLICY_PREFIX = "live-cert-"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class LiveConfig:
    """The five live upstreams, plus the local InterLock stack they run against."""

    # PostgreSQL
    pg_host: str = ""
    pg_port: int = 5432
    pg_database: str = ""
    pg_users: dict[str, str] = field(default_factory=dict)

    # MySQL
    mysql_host: str = ""
    mysql_port: int = 3306
    mysql_database: str = ""
    mysql_users: dict[str, str] = field(default_factory=dict)

    # S3
    s3_bucket: str = ""
    s3_prefix: str = ""
    s3_region: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    # Slack
    slack_bot_token: str = ""
    slack_channel_id: str = ""
    slack_channel_name: str = ""

    # Google Workspace
    gws_service_account_json: str = ""
    gws_subject_user: str = ""
    gws_workspace_domain: str = ""
    gws_client_id: str = ""
    gws_client_email: str = ""
    gws_drive_folder_id: str = ""
    # Located by name when no id is configured. A name is what an operator
    # actually shares, and an id pasted into the environment goes stale the
    # moment the folder is recreated.
    gws_drive_folder_name: str = "live-cert-test"

    # The local stack under test.
    e2e: E2EConfig = field(default_factory=load_e2e_config)

    run_id: str = RUN_ID

    # -- availability predicates -------------------------------------------
    # Each live test skips on its own system's predicate, so a run with only
    # some credentials present certifies exactly what it can.

    def has_postgres(self) -> bool:
        return bool(self.pg_host and self.pg_database and self.pg_users)

    def has_mysql(self) -> bool:
        return bool(self.mysql_host and self.mysql_database and self.mysql_users)

    def has_s3(self) -> bool:
        return bool(self.s3_bucket and self.s3_access_key_id and self.s3_secret_access_key)

    def has_slack(self) -> bool:
        return bool(self.slack_bot_token and self.slack_channel_id)

    def has_google_workspace(self) -> bool:
        """Only requires the key itself.

        Impersonation is a separate capability - see `gws_can_impersonate`.
        A service account with no delegation can still read Drive items shared
        directly with it, which is a certifiable configuration.
        """
        return bool(self.gws_service_account_json)

    def gws_can_impersonate(self) -> bool:
        """Whether domain-wide delegation is even possible for this subject.

        Delegation is granted in a Google Workspace admin console. A consumer
        `@gmail.com` account has no admin console, so a service account can
        never impersonate one, regardless of configuration. Recognising that
        here turns an opaque `unauthorized_client` at call time into an honest
        skip with a stated reason.
        """
        subject = self.gws_subject_user
        if not subject or "@" not in subject:
            return False
        domain = subject.split("@", 1)[1].lower()
        return domain not in {"gmail.com", "googlemail.com"}

    # -- derived names ------------------------------------------------------

    @property
    def table_name(self) -> str:
        """Run-stamped table, safe as a bare SQL identifier."""
        return f"{ARTIFACT_PREFIX}_{self.run_id}"

    @property
    def s3_run_prefix(self) -> str:
        return f"{self.s3_prefix}{ARTIFACT_PREFIX_DASHED}/{self.run_id}/"

    @property
    def slack_marker(self) -> str:
        """Prefix on every message this run posts, so a sweeper can find them."""
        return f"[{ARTIFACT_PREFIX_DASHED} {self.run_id}]"

    def pg_password(self, user: str) -> str:
        return self.pg_users.get(user, "")

    def mysql_password(self, user: str) -> str:
        return self.mysql_users.get(user, "")


def _users_from_env(system: str) -> dict[str, str]:
    """Recover the per-user passwords exported by the credential loader.

    The loader writes `INTERLOCK_LIVE_<SYSTEM>_PASSWORD_<USER>`; this reverses
    that, so the alias names come from the credential file rather than a
    hardcoded list that would drift from it.
    """
    marker = f"INTERLOCK_LIVE_{system}_PASSWORD_"
    return {
        key[len(marker) :].lower(): value
        for key, value in os.environ.items()
        if key.startswith(marker) and value
    }


def load_live_config() -> LiveConfig:
    return LiveConfig(
        pg_host=_env("INTERLOCK_LIVE_PG_HOST"),
        pg_port=_env_int("INTERLOCK_LIVE_PG_PORT", 5432),
        pg_database=_env("INTERLOCK_LIVE_PG_DATABASE"),
        pg_users=_users_from_env("PG"),
        mysql_host=_env("INTERLOCK_LIVE_MYSQL_HOST"),
        mysql_port=_env_int("INTERLOCK_LIVE_MYSQL_PORT", 3306),
        mysql_database=_env("INTERLOCK_LIVE_MYSQL_DATABASE"),
        mysql_users=_users_from_env("MYSQL"),
        s3_bucket=_env("INTERLOCK_LIVE_S3_BUCKET"),
        s3_prefix=_env("INTERLOCK_LIVE_S3_PREFIX"),
        s3_region=_env("INTERLOCK_LIVE_S3_REGION", "us-east-1"),
        s3_access_key_id=_env("INTERLOCK_LIVE_S3_ACCESS_KEY_ID"),
        s3_secret_access_key=_env("INTERLOCK_LIVE_S3_SECRET_ACCESS_KEY"),
        slack_bot_token=_env("INTERLOCK_LIVE_SLACK_BOT_TOKEN"),
        slack_channel_id=_env("INTERLOCK_LIVE_SLACK_CHANNEL_ID"),
        slack_channel_name=_env("INTERLOCK_LIVE_SLACK_CHANNEL_NAME"),
        gws_service_account_json=_env("INTERLOCK_LIVE_GWS_SERVICE_ACCOUNT_JSON"),
        gws_subject_user=_env("INTERLOCK_LIVE_GWS_SUBJECT_USER"),
        gws_workspace_domain=_env("INTERLOCK_LIVE_GWS_WORKSPACE_DOMAIN"),
        gws_client_id=_env("INTERLOCK_LIVE_GWS_CLIENT_ID"),
        gws_client_email=_env("INTERLOCK_LIVE_GWS_CLIENT_EMAIL"),
        gws_drive_folder_id=_env("INTERLOCK_LIVE_GWS_DRIVE_FOLDER_ID"),
        gws_drive_folder_name=_env("INTERLOCK_LIVE_GWS_DRIVE_FOLDER", "live-cert-test"),
    )
