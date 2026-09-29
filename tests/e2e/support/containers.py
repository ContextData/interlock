"""Control compose services from a test, and always put them back.

Two existing e2e files shell out to Docker - one to clear Redis counters, one
to read gateway logs - each with its own inline helper. This is the shared
version, and the first to *stop* a service rather than just talk to one.

That difference is why teardown matters here more than usual. A failed restart
does not fail one test; it poisons every later test in the session, because
`make e2e` does not bring the stack back up between files. So every helper
that stops something is paired with a context manager that restarts it in a
`finally`, and readiness is waited for rather than assumed.

Container names follow from the compose project (`interlock-e2e`), matching
the convention the existing helpers already hardcode.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

PROJECT = "interlock-e2e"

GATEWAY = "gateway"
ADMIN = "admin"
CONTROL_POSTGRES = "postgres"
REDIS = "redis"


class ContainerControlError(RuntimeError):
    """A container operation failed in a way a test cannot reasonably continue past."""


def container(service: str) -> str:
    return f"{PROJECT}-{service}-1"


# Every docker call is bounded. Without this a slow or wedged daemon stalls the
# test run indefinitely rather than failing, which is exactly what happened:
# `docker restart` on the gateway takes the full 30s stop_grace_period because
# the audit buffer does not shut down cleanly, and with several restarts in a
# module the suite appeared to hang with no output and no clue why. A helper
# that can block forever turns a slow dependency into an unbounded stall.
_DEFAULT_TIMEOUT = 30.0
# Stopping or restarting a container waits out its stop_grace_period, which is
# 30s for the gateway, so these need materially more room than a plain call.
_LIFECYCLE_TIMEOUT = 120.0


def _docker(
    *args: str, check: bool = False, timeout: float = _DEFAULT_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    """Run one docker command. argv list, never a shell string, always bounded."""
    try:
        proc = subprocess.run(
            ["docker", *args], capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise ContainerControlError(
            f"docker {' '.join(args)} did not return within {timeout}s. The daemon may be "
            "wedged, or a container is not honouring its stop grace period."
        ) from exc
    if check and proc.returncode != 0:
        raise ContainerControlError(
            f"docker {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()[:300]}"
        )
    return proc


def is_running(service: str) -> bool:
    proc = _docker("inspect", "-f", "{{.State.Running}}", container(service))
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def stop(service: str) -> None:
    """Stop a service with SIGTERM, letting it shut down cleanly."""
    _docker("stop", container(service), check=True, timeout=_LIFECYCLE_TIMEOUT)


def start(service: str) -> None:
    _docker("start", container(service), check=True, timeout=_LIFECYCLE_TIMEOUT)


def restart(service: str) -> None:
    """Restart a service, waiting out its stop grace period.

    Named rather than left as a bare `_docker("restart", ...)` at call sites,
    so the longer bound is applied consistently.
    """
    _docker("restart", container(service), check=True, timeout=_LIFECYCLE_TIMEOUT)


def kill(service: str, signal: str = "KILL") -> None:
    """Stop a service *without* a clean shutdown.

    Used to prove what survives a process that had no chance to flush. A
    graceful stop would let the audit buffer drain, which is the opposite of
    what such a test is asking.
    """
    _docker("kill", "-s", signal, container(service), check=True)


def exec_in(service: str, *command: str) -> str:
    """Run a command inside a container and return its combined output."""
    proc = _docker("exec", container(service), *command)
    return proc.stdout + proc.stderr


def read_file(service: str, path: str) -> str:
    """File contents, or empty string when the file does not exist.

    Absence and emptiness are deliberately the same here: a spool that has
    never been written and one that has been fully drained are both "nothing
    pending", and callers assert on line counts rather than existence.
    """
    proc = _docker("exec", container(service), "sh", "-c", f"cat {path} 2>/dev/null || true")
    return proc.stdout


def count_lines(service: str, path: str) -> int:
    return len([line for line in read_file(service, path).splitlines() if line.strip()])


def wait_until_healthy(url: str, timeout_seconds: float = 90.0) -> None:
    """Poll an endpoint until it answers, rather than sleeping a guessed interval.

    A fixed sleep is how a container-restart test becomes flaky: it passes on
    a warm machine and fails on a cold one, and the failure looks like the
    behaviour under test.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error = "never attempted"
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=3).status_code == 200:
                return
            last_error = "non-200"
        except Exception as exc:  # noqa: BLE001 - the point is to keep retrying
            last_error = f"{type(exc).__name__}"
        time.sleep(0.5)
    raise ContainerControlError(
        f"{url} did not become healthy within {timeout_seconds}s (last: {last_error})"
    )


def wait_for_postgres(service: str = CONTROL_POSTGRES, timeout_seconds: float = 60.0) -> None:
    """Wait for a Postgres container to accept connections again."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        proc = _docker("exec", container(service), "pg_isready", "-U", "onyx", "-d", "onyx")
        if proc.returncode == 0:
            return
        time.sleep(0.5)
    raise ContainerControlError(f"{service} did not accept connections within {timeout_seconds}s")


@contextmanager
def stopped(service: str, *, restore_wait_url: str | None = None) -> Iterator[None]:
    """Stop a service for the duration of the block, and restart it afterwards.

    The restart runs in a `finally` and is not conditional on the block
    succeeding. A test that fails while a dependency is down must not leave it
    down for everything that follows.
    """
    stop(service)
    try:
        yield
    finally:
        start(service)
        if service == CONTROL_POSTGRES:
            wait_for_postgres(service)
        if restore_wait_url:
            wait_until_healthy(restore_wait_url)


def clear_redis_sessions() -> None:
    """Drop cached auth sessions.

    Identity rows outlive a Postgres restart, but a session cached against a
    now-unreachable database is a different question from the one a durability
    test is asking.
    """
    _docker(
        "exec",
        container(REDIS),
        "sh",
        "-c",
        "redis-cli --scan --pattern 'session:*' | xargs -r redis-cli DEL",
    )
