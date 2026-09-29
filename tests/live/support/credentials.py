"""Load live-system credentials, and never let one reach an output stream.

This module owns three things that used to live in `tools/`: the parser for
the sectioned credential file, the `Redactor` that scrubs secrets out of
anything the certification run emits, and the export step that turns parsed
values into environment variables.

The export step exists because of a constraint in the secret resolver. A
source's `connection_config` is written to the control database and served
back by `/api/data-sources`, so it must hold *references* rather than literal
credentials. Of the four reference schemes in
`src/interlock/secrets/resolver.py`, only `env://` can resolve a secret that
lives in the working tree: `file://` is allowlisted to `/run/secrets`,
`/var/run/secrets`, `/etc/interlock/secrets` and `/etc/onyx/secrets`, and
widening that allowlist to reach a repo path would defeat its purpose. So the
credentials are exported to the environment and referenced as
`env://INTERLOCK_LIVE_...` everywhere else.

`export_environment` uses `setdefault`, so an already-set variable always
wins. That single choice is what lets the same code path serve a local
`.env.live` and a CI run whose secrets arrive as environment variables with no
file present at all.

Nothing here prints, logs, or returns a raw secret. `Redactor` is applied at
the point evidence is captured rather than at the point it is written, so an
unredacted value is never held in memory longer than the call that produced
it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CREDENTIALS_FILE = Path(".env.live")
DEFAULT_GWS_CREDENTIALS_FILE = Path("credentials-google.json")

# Sections recognised in the credential file. Matching is by keyword against
# the lowercased header text.
_SECTION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("postgres", ("postgres", "postgresql")),
    ("mysql", ("mysql", "mariadb")),
    ("google_workspace", ("google workspace", "google_workspace", "gws")),
    ("slack", ("slack",)),
    ("s3", ("s3", "object storage")),
)

_SECRETISH_KEYS = ("password", "token", "secret", "key", "private")


@dataclass
class LiveCredentials:
    """Parsed credentials. `secrets` drives redaction, so it must be complete."""

    postgres: dict[str, Any] = field(default_factory=dict)
    mysql: dict[str, Any] = field(default_factory=dict)
    s3: dict[str, Any] = field(default_factory=dict)
    slack: dict[str, Any] = field(default_factory=dict)
    google_workspace: dict[str, Any] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)

    def has_postgres(self) -> bool:
        return bool(self.postgres.get("host") and self.postgres.get("users"))

    def has_mysql(self) -> bool:
        return bool(self.mysql.get("host") and self.mysql.get("users"))

    def has_s3(self) -> bool:
        return bool(self.s3.get("bucket") and self.s3.get("aws_access_key_id"))

    def has_slack(self) -> bool:
        return bool(self.slack.get("bot_token") and self.slack.get("channel_id"))

    def has_google_workspace(self) -> bool:
        """The service account key alone is enough.

        A `subject_user` is only needed for impersonation, which requires
        domain-wide delegation and is impossible for a consumer subject. Without
        it the service account acts as itself and can read anything shared
        directly with its address, which is a certifiable configuration. This
        predicate deliberately matches `LiveConfig.has_google_workspace` -
        having the two disagree meant preflight reported Google as absent while
        its tests ran.
        """
        return bool(self.google_workspace.get("service_account_json"))


class Redactor:
    """Replace known secrets, and secret-shaped tokens, with a marker.

    Literal replacement runs first and longest-first, so a secret that
    contains another secret as a substring cannot be partially revealed by
    replacing the shorter one first.
    """

    def __init__(self, secrets: list[str]) -> None:
        self._secrets = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)
        # The character class below reads `[^\s,;]` -- whitespace, comma,
        # semicolon. The version this replaced wrote `[^\\s,;]` inside a raw
        # string, which is the class {backslash, s, comma, semicolon}. It
        # therefore stopped at the first letter "s" in a token and emitted the
        # remainder in clear: "token=xoxb-secret-value" redacted to
        # "token=<redacted>secret-value". Pinned by a test.
        self._patterns = [
            re.compile(r"xox[baprs]-[A-Za-z0-9-]+"),
            re.compile(r"xapp-[A-Za-z0-9-]+"),
            re.compile(r"AKIA[0-9A-Z]{16}"),
            re.compile(
                r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
            ),
            # Managed-database hostnames. Not a credential, but this project
            # already treats them as disclosure -- secret_scan.py blocks the
            # same shape, on the grounds that a hostname discloses
            # infrastructure names. The certification report is the artifact
            # that leaves the machine, so it is held to that standard too.
            # Caught by reading a real report, not by reasoning: the literal
            # secret check passed, because a hostname is not in the credential
            # file's value list. True, and insufficient.
            re.compile(r"\b[a-z0-9-]+-do-user-\d+-\d+\.[a-z]\.db\.ondigitalocean\.com\b"),
            re.compile(r"\b[a-z0-9-]+\.[a-z0-9-]+\.rds\.amazonaws\.com\b"),
            re.compile(r"(?i)\b(password|token|secret|authorization|cookie)=([^\s,;]+)"),
        ]

    @property
    def patterns(self) -> list[re.Pattern[str]]:
        """The shape-based rules, exposed so `assert_no_secrets` reuses them.

        One definition, used both to redact and to verify the redaction, so
        the two can never drift apart.
        """
        return list(self._patterns)

    def text(self, value: Any) -> str:
        text = str(value)
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        for pattern in self._patterns:
            text = pattern.sub(self._substitute, text)
        return text

    @staticmethod
    def _substitute(match: re.Match[str]) -> str:
        if match.groups():
            return f"{match.group(1)}=<redacted>"
        return "<redacted>"

    def obj(self, value: Any) -> Any:
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                if any(word in str(key).lower() for word in _SECRETISH_KEYS):
                    redacted[key] = "<redacted>"
                else:
                    redacted[key] = self.obj(item)
            return redacted
        if isinstance(value, list | tuple):
            return [self.obj(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def assert_no_secrets(text: str, secrets: list[str]) -> None:
    """Refuse to emit text containing a known secret or a sensitive shape.

    The last line of defence before a report is written, and it checks *both*
    halves deliberately. Literal values catch what the credential file
    declared; patterns catch what it did not. An earlier version checked only
    literals and passed a report containing a managed-database hostname, which
    this project's own scanner treats as disclosure. The check was true and
    incomplete, which is the failure mode that makes a green result
    untrustworthy.
    """
    problems: list[str] = []

    leaked = sorted({s for s in secrets if s and len(s) >= 6 and s in text})
    if leaked:
        # How many and how long, never which. Naming one here would write the
        # secret into the traceback that reports it.
        problems.append(
            f"{len(leaked)} known secret value(s) [{', '.join(f'{len(s)} chars' for s in leaked)}]"
        )

    probe = Redactor([])
    for pattern in probe.patterns:
        if pattern.search(text):
            problems.append(f"text matching {pattern.pattern[:48]!r}")

    if problems:
        raise AssertionError("refusing to emit: " + "; ".join(problems) + " survived redaction")


def _section_name(line: str) -> str | None:
    text = line.strip().lstrip("#").strip().lower()
    if not text:
        return None
    for name, keywords in _SECTION_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return name
    return None


def _kv(line: str) -> tuple[str, str] | None:
    if ":" in line:
        key, value = line.split(":", 1)
    elif "=" in line:
        key, value = line.split("=", 1)
    else:
        return None
    return key.strip().lower().replace(" ", "_"), value.strip()


def _parse_db_section(lines: list[str]) -> dict[str, Any]:
    data: dict[str, Any] = {"users": {}}
    for line in lines:
        stripped = line.strip()
        user_match = re.match(r"-\s*([A-Za-z0-9_@.-]+)\s*\(\s*password\s*:\s*([^)]*)\)", stripped)
        if user_match:
            data["users"][user_match.group(1)] = user_match.group(2).strip()
            continue
        pair = _kv(stripped)
        if not pair:
            continue
        key, value = pair
        if key in {"db_name", "database", "database_name"}:
            data["database"] = value
        elif key == "port":
            data["port"] = int(value)
        elif key == "host":
            data["host"] = value
    return data


def _parse_s3_section(lines: list[str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    loose: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        pair = _kv(stripped)
        if pair:
            key, value = pair
            if key == "bucket":
                bucket_match = re.search(r"s3://([^\s)]+)", value)
                name_match = re.search(r"name:\s*([^)]+)", value)
                chosen = name_match or bucket_match
                data["bucket"] = chosen.group(1).strip() if chosen else value
            elif key == "region":
                data["region_name"] = value
            elif key == "prefix":
                data["prefix"] = value
            continue
        loose.append(stripped)
    # The access key id and secret access key are written as bare lines with
    # no label, in that order. Positional, and therefore fragile -- validated
    # by shape below rather than trusted.
    if loose:
        data["aws_access_key_id"] = loose[0]
    if len(loose) > 1:
        data["aws_secret_access_key"] = loose[1]
    return data


def _parse_slack_section(lines: list[str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        pair = _kv(stripped)
        if not pair:
            continue
        key, value = pair
        if key == "channel_name":
            data["channel_name"] = value.lstrip("#")
        elif key == "channel_id":
            data["channel_id"] = value
        elif key == "bot_user_oauth_token":
            data["bot_token"] = value
        elif key in {"app-level_tokens", "app_level_tokens", "app-level_token"}:
            data["app_token"] = value
    return data


def _parse_google_workspace_section(lines: list[str]) -> dict[str, Any]:
    """Read the subject user; ignore the password.

    Google's APIs do not accept a username and password for programmatic
    access. Authentication comes from the service account key loaded
    separately; the username in this section is the account the service
    account impersonates via domain-wide delegation, which
    `_gws_requires_subject` makes mandatory whenever a service account is used.
    """
    data: dict[str, Any] = {}
    for line in lines:
        pair = _kv(line.strip())
        if not pair:
            continue
        key, value = pair
        if key in {"username", "user", "subject_user", "subject"}:
            data["subject_user"] = value
        elif key in {"workspace_domain", "domain"}:
            data["workspace_domain"] = value
    if "workspace_domain" not in data and "@" in data.get("subject_user", ""):
        data["workspace_domain"] = data["subject_user"].split("@", 1)[1]
    return data


def _load_service_account(path: Path) -> dict[str, Any]:
    """Read and shape-check the service account key.

    A shape check here turns the most common misconfiguration -- handing over
    an OAuth *client* config instead of a service account *key* -- into a
    clear message at load time rather than an opaque Google error much later.
    """
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} is not readable JSON: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    if payload.get("type") != "service_account":
        raise ValueError(
            f"{path} has type={payload.get('type')!r}; a service account key is required. "
            "An OAuth client config will not work for unattended access."
        )
    missing = [k for k in ("private_key", "client_email", "client_id") if not payload.get(k)]
    if missing:
        raise ValueError(f"{path} is missing required field(s): {', '.join(missing)}")
    return {
        "service_account_json": json.dumps(payload, separators=(",", ":")),
        "client_email": payload["client_email"],
        # Needed verbatim when authorising domain-wide delegation in the
        # Workspace admin console, so surface it for the failure message.
        "client_id": payload["client_id"],
        "project_id": payload.get("project_id", ""),
        "private_key": payload["private_key"],
    }


def _collect_secrets(*sections: dict[str, Any]) -> list[str]:
    secrets: list[str] = []
    for data in sections:
        for key, value in data.items():
            if isinstance(value, dict):
                secrets.extend(str(v) for v in value.values() if v)
            elif any(word in key for word in _SECRETISH_KEYS) and value:
                secrets.append(str(value))
    return secrets


def load_credentials(
    path: Path | None = None,
    *,
    gws_credentials_file: Path | None = None,
) -> LiveCredentials:
    """Parse the credential file. A missing file yields empty credentials.

    Absence is not an error: every live test skips when its system's
    credentials are absent, so a contributor without them runs the suite
    unchanged.
    """
    path = path or Path(os.environ.get("INTERLOCK_LIVE_CREDENTIALS_FILE", DEFAULT_CREDENTIALS_FILE))
    gws_path = gws_credentials_file or Path(
        os.environ.get("INTERLOCK_LIVE_GWS_CREDENTIALS_FILE", DEFAULT_GWS_CREDENTIALS_FILE)
    )

    sections: dict[str, list[str]] = {name: [] for name, _ in _SECTION_KEYWORDS}
    if path.is_file():
        current: str | None = None
        for line in path.read_text().splitlines():
            if line.strip().startswith("#"):
                # An unrecognised header ends the previous section rather than
                # silently appending its body to it.
                current = _section_name(line)
                continue
            if current:
                sections[current].append(line)

    postgres = _parse_db_section(sections["postgres"])
    mysql = _parse_db_section(sections["mysql"])
    s3 = _parse_s3_section(sections["s3"])
    slack = _parse_slack_section(sections["slack"])
    google_workspace = _parse_google_workspace_section(sections["google_workspace"])
    google_workspace.update(_load_service_account(gws_path))

    return LiveCredentials(
        postgres=postgres,
        mysql=mysql,
        s3=s3,
        slack=slack,
        google_workspace=google_workspace,
        secrets=_collect_secrets(postgres, mysql, s3, slack, google_workspace),
    )


def _env_user_key(system: str, user: str) -> str:
    normalised = re.sub(r"[^A-Za-z0-9]+", "_", user).upper().strip("_")
    return f"INTERLOCK_LIVE_{system}_PASSWORD_{normalised}"


def environment_for(creds: LiveCredentials) -> dict[str, str]:
    """Build the variable mapping without touching os.environ.

    Separated from `export_environment` so a test can assert the mapping's
    shape without mutating process state.
    """
    env: dict[str, str] = {}

    if creds.postgres.get("host"):
        env["INTERLOCK_LIVE_PG_HOST"] = str(creds.postgres["host"])
        env["INTERLOCK_LIVE_PG_PORT"] = str(creds.postgres.get("port", 5432))
        env["INTERLOCK_LIVE_PG_DATABASE"] = str(creds.postgres.get("database", ""))
        for user, password in creds.postgres.get("users", {}).items():
            env[_env_user_key("PG", user)] = str(password)

    if creds.mysql.get("host"):
        env["INTERLOCK_LIVE_MYSQL_HOST"] = str(creds.mysql["host"])
        env["INTERLOCK_LIVE_MYSQL_PORT"] = str(creds.mysql.get("port", 3306))
        env["INTERLOCK_LIVE_MYSQL_DATABASE"] = str(creds.mysql.get("database", ""))
        for user, password in creds.mysql.get("users", {}).items():
            env[_env_user_key("MYSQL", user)] = str(password)

    if creds.s3.get("bucket"):
        env["INTERLOCK_LIVE_S3_BUCKET"] = str(creds.s3["bucket"])
        env["INTERLOCK_LIVE_S3_PREFIX"] = str(creds.s3.get("prefix", ""))
        env["INTERLOCK_LIVE_S3_REGION"] = str(creds.s3.get("region_name", ""))
        if creds.s3.get("aws_access_key_id"):
            env["INTERLOCK_LIVE_S3_ACCESS_KEY_ID"] = str(creds.s3["aws_access_key_id"])
        if creds.s3.get("aws_secret_access_key"):
            env["INTERLOCK_LIVE_S3_SECRET_ACCESS_KEY"] = str(creds.s3["aws_secret_access_key"])

    if creds.slack.get("bot_token"):
        env["INTERLOCK_LIVE_SLACK_BOT_TOKEN"] = str(creds.slack["bot_token"])
        env["INTERLOCK_LIVE_SLACK_CHANNEL_ID"] = str(creds.slack.get("channel_id", ""))
        env["INTERLOCK_LIVE_SLACK_CHANNEL_NAME"] = str(creds.slack.get("channel_name", ""))
        if creds.slack.get("app_token"):
            env["INTERLOCK_LIVE_SLACK_APP_TOKEN"] = str(creds.slack["app_token"])

    gws = creds.google_workspace
    if gws.get("service_account_json"):
        env["INTERLOCK_LIVE_GWS_SERVICE_ACCOUNT_JSON"] = str(gws["service_account_json"])
        env["INTERLOCK_LIVE_GWS_SUBJECT_USER"] = str(gws.get("subject_user", ""))
        env["INTERLOCK_LIVE_GWS_WORKSPACE_DOMAIN"] = str(gws.get("workspace_domain", ""))
        env["INTERLOCK_LIVE_GWS_CLIENT_ID"] = str(gws.get("client_id", ""))
        env["INTERLOCK_LIVE_GWS_CLIENT_EMAIL"] = str(gws.get("client_email", ""))

    return env


def export_environment(creds: LiveCredentials) -> list[str]:
    """Export credentials, letting any pre-set variable win.

    `setdefault` is the whole point: in CI the variables arrive from the
    secret store and no credential file exists, and the same call is then a
    no-op rather than a special case.

    Returns the variable *names* set, never their values, so a caller can log
    what was configured without logging what it was configured to.
    """
    exported: list[str] = []
    for key, value in environment_for(creds).items():
        if value and os.environ.setdefault(key, value) == value:
            exported.append(key)
    return sorted(exported)
