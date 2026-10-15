"""Console (admin) roles and the user types offered when adding a console user.

One definition, used by the RBAC middleware, the SSO login allowlist, the
console-user pages and the generated admin-roles reference.
"""

from __future__ import annotations

from collections.abc import Iterable

# Roles that may do everything, including managing console users.
CONSOLE_ADMIN_ROLES = frozenset({"owner", "admin"})

ROLE_MEANINGS: dict[str, str] = {
    "owner": "Everything, including admin accounts. The first admin is an owner.",
    "admin": "Everything, like owner.",
    "security_admin": "Identities, keys and grants; any mutation without a narrower rule.",
    "source_admin": "Data sources, connectors, source roles, catalog and ingestion.",
    "policy_admin": "Policy rules.",
    "approval_reviewer": "Reviewing queued writes: approve and reject.",
    "auditor": "Read-only access to the console, audit and usage.",
    "viewer": (
        "Read-only: every console page and read API except console users; "
        "cannot change anything, including dry-runs and connection tests."
    ),
}

ADMIN_ROLES = frozenset(ROLE_MEANINGS)

# Roles an administrator may pick one by one for a Custom console user.
# `owner` and `admin` are offered only as the Administrator type.
CUSTOM_ROLES: tuple[str, ...] = (
    "security_admin",
    "source_admin",
    "policy_admin",
    "approval_reviewer",
    "auditor",
    "viewer",
)

# User type -> (label, roles saved for it). Custom is chosen role by role.
USER_TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "administrator": ("Administrator", ("owner",)),
    "read_write": ("Read-write", ("security_admin",)),
    "read_only": ("Read-only", ("viewer",)),
}
CUSTOM_TYPE = "custom"


def normalize_roles(roles: Iterable[str]) -> set[str]:
    return {str(role).strip().lower() for role in roles if str(role).strip()}


def is_console_admin(roles: Iterable[str]) -> bool:
    return bool(normalize_roles(roles) & CONSOLE_ADMIN_ROLES)


def is_read_only(roles: Iterable[str]) -> bool:
    held = normalize_roles(roles)
    return bool(held) and held <= {"viewer"}


def classify_roles(roles: Iterable[str]) -> tuple[str, str]:
    """(user type key, label) for a stored role list."""
    held = normalize_roles(roles)
    if held & CONSOLE_ADMIN_ROLES:
        return "administrator", "Administrator"
    for key, (label, type_roles) in USER_TYPES.items():
        if held == set(type_roles):
            return key, label
    return CUSTOM_TYPE, ("Custom: " + ", ".join(sorted(held))) if held else "No roles"
