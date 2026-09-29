"""Bring the compose stack up with live credentials in the gateway's environment.

The governed live sources hold `env://` references, and the gateway resolves
them inside its container. This module loads the credential file, exports the
values into its own environment, and execs `docker compose` so the child
inherits them.

Doing it here rather than in the Makefile keeps the values out of shell
history, out of the process command line, and out of any file: the only
representation is the environment of this process and the containers it
starts.

    uv run python -m tests.live.support.stack up
    uv run python -m tests.live.support.stack down
    uv run python -m tests.live.support.stack status
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from tests.live.support.credentials import export_environment, load_credentials

PROJECT = "interlock-e2e"
FILES = ("docker-compose.yml", "docker-compose.e2e.yml", "docker-compose.live.yml")

# The variables the containers need. Kept explicit rather than exporting
# everything, so adding a credential to the stack is a visible decision.
REQUIRED = (
    "INTERLOCK_LIVE_PG_PASSWORD_GENERIC_WRITE_USER",
    "INTERLOCK_LIVE_MYSQL_PASSWORD_GENERIC_FULL_USER",
    "INTERLOCK_LIVE_S3_ACCESS_KEY_ID",
    "INTERLOCK_LIVE_S3_SECRET_ACCESS_KEY",
    "INTERLOCK_LIVE_SLACK_BOT_TOKEN",
    "INTERLOCK_LIVE_GWS_SERVICE_ACCOUNT_JSON",
)


def _compose(*args: str) -> list[str]:
    command = ["docker", "compose", "-p", PROJECT]
    for name in FILES:
        command += ["-f", name]
    return command + list(args)


def _prepare_environment() -> list[str]:
    """Export credentials and report which required variables are still absent."""
    export_environment(load_credentials())
    # Compose warns loudly about unset pass-through variables; set the absent
    # ones to empty so a partial credential set degrades to "that source is
    # not configured" rather than a wall of warnings.
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    for name in missing:
        os.environ.setdefault(name, "")
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("up", "down", "status", "restart-gateway"))
    args = parser.parse_args(argv)

    missing = _prepare_environment()
    if missing:
        print(f"warning: {len(missing)} credential(s) absent; their sources will be skipped:")
        for name in missing:
            print(f"  - {name}")

    if args.action == "up":
        command = _compose("up", "-d")
    elif args.action == "down":
        command = _compose("down")
    elif args.action == "restart-gateway":
        # Sources are read at startup; a config change needs the gateway to
        # pick up new environment values.
        command = _compose("up", "-d", "--force-recreate", "gateway", "admin")
    else:
        command = _compose("ps")

    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
