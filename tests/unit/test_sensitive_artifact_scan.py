from __future__ import annotations

import subprocess
from pathlib import Path

from interlock.security.secret_scan import scan


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def test_sensitive_artifact_scan_passes_clean_tracked_files(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("env://GOOGLE_SERVICE_ACCOUNT_JSON is a safe placeholder\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)

    assert scan(tmp_path) == []


def test_sensitive_artifact_scan_flags_known_live_credential_pattern(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    report = tmp_path / "reference.md"
    report.write_text("temporary key " + "AKIA" + "ABCDEFGHIJKLMNOP")
    subprocess.run(["git", "add", "reference.md"], cwd=tmp_path, check=True)

    findings = scan(tmp_path)

    assert findings == ["reference.md:1: contains blocked sensitive pattern"]


def test_sensitive_artifact_scan_flags_untracked_commit_candidate(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    report = tmp_path / "untracked.md"
    report.write_text("temporary key " + "AKIA" + "ABCDEFGHIJKLMNOP")

    assert scan(tmp_path) == ["untracked.md:1: contains blocked sensitive pattern"]


def test_sensitive_artifact_scan_respects_gitignore(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    (tmp_path / "ignored.txt").write_text("temporary key " + "AKIA" + "ABCDEFGHIJKLMNOP")

    assert scan(tmp_path) == []


def test_sensitive_artifact_scan_flags_current_credential_shapes(tmp_path: Path) -> None:
    """Each pattern class must actually fire; a scanner that only passes is useless."""
    _init_repo(tmp_path)
    cases = {
        "aws.txt": "AKIA" + "ABCDEFGHIJKLMNOP",
        "anthropic.txt": "sk-ant-" + "api03-" + "A" * 24,
        "github.txt": "ghp_" + "B" * 36,
        "gitlab.txt": "glpat-" + "C" * 20,
        "do_token.txt": "dop_v1_" + "a1b2c3d4" * 8,
        "db_password.txt": "AVNS_" + "aBcDeFgHiJkLmNoP",
        "managed_host.txt": "cluster-do-user-12345-0.k.db.ondigitalocean.com",  # secret-scan: allow - synthetic, asserts the pattern fires
        "dsn.txt": "postgresql://admin:" + "SuperSecret123" + "@prod.example.com/db",
        "cache_url.txt": "rediss://:" + "a1b2c3d4e5f6" * 4 + "@cache.example.com:6379/0",
        "rds_host.txt": "interlock-control-db." + "c1a2b3c4d5e6" + ".us-east-1.rds.amazonaws.com",
        "cache_host.txt": "master.interlock-valkey." + "ab12cd" + ".use1.cache.amazonaws.com",
        "private_key.txt": "-----BEGIN PRIVATE KEY-----\n" + "MIIEvQIBADANBgkq" * 4,
    }
    for name, body in cases.items():
        (tmp_path / name).write_text(body + "\n")

    flagged = {finding.split(":", 1)[0] for finding in scan(tmp_path)}

    assert flagged == set(cases), f"missed: {set(cases) - flagged}"


def test_sensitive_artifact_scan_allows_unsubstituted_uri_placeholders(tmp_path: Path) -> None:
    """A connection URI whose password is a placeholder is not a credential.

    This is the documented, correct way to write a connection string, and the
    operator guide does exactly that. Flagging it pressures doc authors into
    inventing literal-looking examples, which is the outcome the scanner
    exists to prevent. Regression: `psql "postgresql://agent:$API_KEY@..."`
    in docs-site/src/content/docs/get-started/setup-walkthrough.md failed `make secret-scan`.
    """
    _init_repo(tmp_path)
    placeholders = {
        "shell.md": "postgresql://agent:$API_KEY@127.0.0.1:5434/sales_pg",
        "braced.md": "postgresql://agent:${API_KEY}@127.0.0.1:5434/sales_pg",
        "angle.md": "mysql://user:<your-password>@db.example.com/app",
        "template.md": "postgresql://user:{{ db_password }}@db.example.com/app",
        "percent.md": "redis://user:%REDIS_PASSWORD%@cache.example.com/0",
        "elasticache.md": "rediss://:$CACHE_AUTH@cache.example.com:6379/0",
    }
    for name, body in placeholders.items():
        (tmp_path / name).write_text(body + "\n")

    assert scan(tmp_path) == []


def test_sensitive_artifact_scan_still_flags_literal_uri_credentials(tmp_path: Path) -> None:
    """The placeholder exemption must not become a hole for real credentials.

    Paired with the test above: narrowing a detection pattern is only safe
    while the thing it detects still fires.
    """
    _init_repo(tmp_path)
    (tmp_path / "leak.md").write_text(
        "postgresql://admin:" + "hunter2SuperSecret" + "@prod.example.com/db\n"
    )

    assert scan(tmp_path) == ["leak.md:1: contains blocked sensitive pattern"]


def test_sensitive_artifact_scan_allows_documented_synthetic_fixture(tmp_path: Path) -> None:
    """A line-scoped pragma lets redaction tests keep their synthetic literals."""
    _init_repo(tmp_path)
    fixture = tmp_path / "test_redaction.py"
    fixture.write_text(
        'KEY = "AKIA' + 'ABCDEFGHIJKLMNOP"  # secret-scan: allow - synthetic fixture\n'
    )

    assert scan(tmp_path) == []


def test_sensitive_artifact_scan_pragma_is_line_scoped(tmp_path: Path) -> None:
    """Allowing one line must not silence a real secret elsewhere in the file."""
    _init_repo(tmp_path)
    fixture = tmp_path / "mixed.py"
    fixture.write_text(
        'SAFE = "AKIA' + 'ABCDEFGHIJKLMNOP"  # secret-scan: allow - synthetic\n'
        'REAL = "AKIA' + 'ZZZZZZZZZZZZZZZZ"\n'
    )

    assert scan(tmp_path) == ["mixed.py:2: contains blocked sensitive pattern"]


def test_sensitive_artifact_scan_does_not_flag_redaction_assertions(tmp_path: Path) -> None:
    """Header-only key literals used to assert redaction are not key material."""
    _init_repo(tmp_path)
    fixture = tmp_path / "test_logs.py"
    fixture.write_text('assert "-----BEGIN PRIVATE KEY-----abc" not in message\n')

    assert scan(tmp_path) == []


def test_sensitive_artifact_scan_ignores_deleted_tracked_paths(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    deleted = tmp_path / "deleted.txt"
    deleted.write_text("safe placeholder\n")
    subprocess.run(["git", "add", "deleted.txt"], cwd=tmp_path, check=True)
    deleted.unlink()

    assert scan(tmp_path) == []
