"""Offline validation of the live certification setup.

Answers "would a live run work, and what would it cover?" without touching a
single upstream system. Cheap enough to run before every certification, and it
catches the failures that otherwise appear thirty seconds into a run against
production: an unparsed credential file, a service account of the wrong type, a
required environment variable that never got exported.

Prints what is configured and what is missing, and never prints a value.

    uv run python -m tests.live.preflight
"""

from __future__ import annotations

import sys

from tests.live.support.credentials import environment_for, load_credentials
from tests.live.support.stack import REQUIRED


def main(argv: list[str] | None = None) -> int:
    try:
        credentials = load_credentials()
    except ValueError as exc:
        # A wrong-shaped service account key is the common case, and its
        # message already says what to do.
        print(f"credential file is unusable: {exc}")
        return 1

    systems = {
        "postgresql": credentials.has_postgres(),
        "mysql": credentials.has_mysql(),
        "s3": credentials.has_s3(),
        "slack": credentials.has_slack(),
        "google_workspace": credentials.has_google_workspace(),
    }

    print("configured systems")
    for name, available in systems.items():
        print(f"  {name:<20} {'yes' if available else 'no - tests will skip'}")

    environment = environment_for(credentials)
    print()
    print(f"environment variables to export: {len(environment)}")
    missing = [name for name in REQUIRED if not environment.get(name)]
    if missing:
        print(f"  {len(missing)} required by the gateway are absent:")
        for name in missing:
            print(f"    - {name}")
    else:
        print("  every variable the gateway needs is present")

    print()
    print(f"secrets registered for redaction: {len(credentials.secrets)}")
    if not credentials.secrets and any(systems.values()):
        print("  WARNING: systems are configured but no secrets were captured.")
        print("  The redactor would have nothing to scrub, so a report could leak.")
        return 1

    gws = credentials.google_workspace
    if systems["google_workspace"]:
        print()
        print("google workspace")
        print(f"  service account : {gws.get('client_email', '(unknown)')}")
        subject = str(gws.get("subject_user", ""))
        domain = subject.split("@")[-1].lower() if "@" in subject else ""
        if not subject:
            print("  mode            : service account acting as itself.")
            print("                    It reads only what is shared directly with the")
            print("                    address above, which covers Drive. Gmail and")
            print("                    Admin Reports need impersonation and are out of")
            print("                    scope in this configuration.")
        elif domain in {"gmail.com", "googlemail.com"}:
            print("  delegation      : impossible - the subject is a consumer account,")
            print("                    which has no Workspace admin console to grant it.")
            print("                    Drive works via direct sharing; Gmail and Admin")
            print("                    Reports are unreachable.")
        else:
            print(f"  delegation      : possible for {domain}; authorise client_id")
            print(f"                    {gws.get('client_id', '?')} for the connector scopes.")

    if not any(systems.values()):
        print()
        print("nothing is configured; a live run would skip everything.")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
