"""Secret resolution.

Phase 5 P5-T05.

Configuration values may be literal strings or references to a secret
backend. References use a URI scheme:

- ``env://NAME`` -> ``os.environ["NAME"]``
- ``file:///mounted/secret`` -> contents of an allowlisted local secret file.
- ``vault://path/key`` -> HashiCorp Vault (KV v2). Optional, requires
  ``hvac`` to be installed and ``VAULT_ADDR`` / ``VAULT_TOKEN`` in env.
- ``aws-sm://name[#json_key]`` -> AWS Secrets Manager. Optional,
  requires ``boto3`` installed and standard AWS credentials.

Anything that does not match a recognised scheme is returned verbatim.
``MissingSecretError`` is raised when a reference cannot be resolved
(rather than returning the literal URI, which would silently treat a
secret as plain text).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)


class MissingSecretError(RuntimeError):
    pass


class SecretBackend(Protocol):
    scheme: str

    def resolve(self, ref: str) -> str: ...


class EnvBackend:
    scheme = "env"

    def resolve(self, ref: str) -> str:
        # ref is "env://NAME"
        parsed = urlsplit(ref)
        # urlsplit treats env://NAME as scheme=env, netloc=NAME.
        name = parsed.netloc or parsed.path.lstrip("/")
        if not name:
            raise MissingSecretError(f"empty env var name in {ref!r}")
        try:
            return os.environ[name]
        except KeyError as exc:
            raise MissingSecretError(f"env var {name!r} not set") from exc


_DEFAULT_FILE_ROOTS = (
    Path("/run/secrets"),
    Path("/var/run/secrets"),
    Path("/etc/interlock/secrets"),
    # Accepted for the rename compatibility window so an operator who already
    # mounted secrets under the old path is not broken by an upgrade. Removal
    # follows the same schedule as the ONYX_* environment prefix.
    Path("/etc/onyx/secrets"),
)


def _configured_file_roots() -> tuple[Path, ...]:
    raw = os.environ.get("INTERLOCK_SECRET_FILE_ROOTS")
    if not raw:
        return _DEFAULT_FILE_ROOTS
    roots = [
        Path(item.strip())
        for item in re.split(r"[,{}]".format(re.escape(os.pathsep)), raw)
        if item.strip()
    ]
    return tuple(roots) or _DEFAULT_FILE_ROOTS


def _validate_file_ref_path(ref: str, *, require_exists: bool) -> Path:
    parsed = urlsplit(ref)
    if parsed.scheme != "file":
        raise MissingSecretError(f"not a file secret ref: {ref!r}")
    if parsed.netloc not in ("", "localhost"):
        raise MissingSecretError("file secret refs must use local absolute paths")
    path = Path(unquote(parsed.path))
    if not path.is_absolute():
        raise MissingSecretError("file secret refs must use absolute paths")
    try:
        resolved_path = path.resolve(strict=require_exists)
    except FileNotFoundError as exc:
        raise MissingSecretError(f"file secret {str(path)!r} not found") from exc
    except OSError as exc:
        raise MissingSecretError(f"file secret {str(path)!r} cannot be resolved") from exc

    allowed_roots = tuple(root.resolve(strict=False) for root in _configured_file_roots())
    if not any(resolved_path.is_relative_to(root) for root in allowed_roots):
        roots = ", ".join(str(root) for root in allowed_roots)
        raise MissingSecretError(f"file secret path is outside allowed roots: {roots}")
    return resolved_path


class FileBackend:
    scheme = "file"

    def resolve(self, ref: str) -> str:
        path = _validate_file_ref_path(ref, require_exists=True)
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MissingSecretError(f"file secret {str(path)!r} cannot be read") from exc


class VaultBackend:
    scheme = "vault"

    def __init__(self) -> None:
        try:
            import hvac  # type: ignore[import-not-found]
        except ImportError as exc:
            raise MissingSecretError("hvac not installed for vault://") from exc
        addr = os.environ.get("VAULT_ADDR")
        token = os.environ.get("VAULT_TOKEN")
        if not addr or not token:
            raise MissingSecretError("VAULT_ADDR / VAULT_TOKEN not set")
        self._client = hvac.Client(url=addr, token=token)

    def resolve(self, ref: str) -> str:
        parsed = urlsplit(ref)
        path = (parsed.netloc + parsed.path).lstrip("/")
        if "/" not in path:
            raise MissingSecretError(f"vault ref must be 'path/key': {ref}")
        secret_path, key = path.rsplit("/", 1)
        result = self._client.secrets.kv.v2.read_secret_version(path=secret_path)
        try:
            return str(result["data"]["data"][key])
        except KeyError as exc:
            raise MissingSecretError(f"vault: key {key!r} not in {secret_path!r}") from exc


class AWSSecretsManagerBackend:
    scheme = "aws-sm"

    def __init__(self) -> None:
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise MissingSecretError("boto3 not installed for aws-sm://") from exc
        self._client = boto3.client("secretsmanager")

    def resolve(self, ref: str) -> str:
        parsed = urlsplit(ref)
        name = parsed.netloc + parsed.path
        json_key = parsed.fragment or None
        resp = self._client.get_secret_value(SecretId=name)
        secret = resp.get("SecretString", "")
        if json_key:
            import json

            try:
                obj = json.loads(secret)
                return str(obj[json_key])
            except (json.JSONDecodeError, KeyError) as exc:
                raise MissingSecretError(
                    f"aws-sm: cannot extract {json_key!r} from {name!r}"
                ) from exc
        return secret


# Lazy backend registry so that importing this module never tries to
# load hvac/boto3 unnecessarily.
_BACKENDS: dict[str, SecretBackend] = {"env": EnvBackend(), "file": FileBackend()}


def _get_backend(scheme: str) -> SecretBackend:
    if scheme in _BACKENDS:
        return _BACKENDS[scheme]
    if scheme == "vault":
        b: SecretBackend = VaultBackend()
    elif scheme == "aws-sm":
        b = AWSSecretsManagerBackend()
    else:
        raise MissingSecretError(f"unknown secret scheme: {scheme}")
    _BACKENDS[scheme] = b
    return b


def resolve(value: str | None) -> str | None:
    """Resolve a value that may or may not be a secret reference."""
    if value is None:
        return None
    if "://" not in value:
        return value
    scheme = value.split("://", 1)[0]
    backend = _get_backend(scheme)
    return backend.resolve(value)


def resolve_file_path(value: str | None) -> str | None:
    """Resolve a value that may point at a mounted secret file path.

    ``file://`` refs are validated against the same allowlist as ``resolve``
    but return the mounted path instead of reading the contents. Other refs are
    resolved normally so env/vault/aws-sm can still supply a path string.
    """
    if value is None:
        return None
    if value.startswith("file://"):
        return str(_validate_file_ref_path(value, require_exists=False))
    resolved = resolve(value)
    return None if resolved is None else str(resolved)
