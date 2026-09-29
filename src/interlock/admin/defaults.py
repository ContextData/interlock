"""The first admin account a fresh install creates."""

from __future__ import annotations

DEFAULT_ADMIN_USERNAME = "admin"
# Documented, deliberately well known, and refused as a new password: an
# account created with it must change it before the console is usable.
DEFAULT_ADMIN_PASSWORD = "admin"  # noqa: S105
