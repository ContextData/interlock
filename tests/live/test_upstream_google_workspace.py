"""Tier 0: the live Google Workspace tenant, before InterLock is involved.

Scope here is narrower than the connector's, and the narrowing is a property
of the environment rather than a choice.

`GoogleWorkspaceAdapter` supports two authentication paths. The one it is
built around is a service account **impersonating** a user via domain-wide
delegation. That is unavailable here: delegation is granted in a Google
Workspace admin console, and the configured subject is a consumer
`@gmail.com` account, which has no admin console. Attempting it returns an
opaque `unauthorized_client`. Established by running it, not by reading the
docs, and encoded in `LiveConfig.gws_can_impersonate` so the affected tests
skip with a stated reason instead of failing mysteriously.

The path that does work is the service account acting as **itself**, reading
what has been explicitly shared with its own address. That covers Drive and
nothing else: a service account has no mailbox, so Gmail is unreachable, and
Admin Reports needs delegation. Those are recorded as UNPROVEN with a reason,
never as passing and never as not-applicable - they are certifiable, just not
in this environment.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.live.support import effects
from tests.live.support.config import LiveConfig, load_live_config
from tests.live.support.evidence import Control, Verdict, record

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not load_live_config().has_google_workspace(),
        reason="live Google Workspace service account is not configured",
    ),
]


@pytest.fixture(scope="module")
def folder_id(live_config: LiveConfig) -> str:
    fid = effects.drive_folder_id(live_config)
    if not fid:
        pytest.skip(
            f"no Drive folder named {live_config.gws_drive_folder_name!r} is shared with "
            f"{live_config.gws_client_email}; share one to certify Drive"
        )
    return fid


@pytest.fixture(scope="module")
def created_files(live_config: LiveConfig) -> Iterator[list[str]]:
    ids: list[str] = []
    try:
        yield ids
    finally:
        for file_id in ids:
            try:
                effects.drive_delete(live_config, file_id)
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                import warnings

                warnings.warn(
                    f"could not delete Drive file {file_id} ({type(exc).__name__})",
                    stacklevel=1,
                )


def test_the_service_account_authenticates(live_config: LiveConfig) -> None:
    who = effects._drive_service(live_config).about().get(fields="user").execute()

    record(
        "google_workspace",
        Control.UPSTREAM,
        Verdict.PASS,
        detail="service account authenticated to Drive as itself",
        identity=who.get("user", {}).get("emailAddress"),
    )
    assert who.get("user"), "Drive returned no identity for the service account"


def test_impersonation_is_unavailable_for_a_consumer_subject(live_config: LiveConfig) -> None:
    """Records *why* the delegated path is unproven, rather than leaving a gap.

    A certification that simply omitted Gmail and Admin Reports would read as
    though nobody thought about them. This states the blocker and what would
    lift it.
    """
    if live_config.gws_can_impersonate():
        pytest.skip("subject is on a Workspace domain; delegation is testable separately")

    record(
        "google_workspace",
        Control.UPSTREAM,
        Verdict.UNPROVEN,
        detail=(
            "domain-wide delegation is impossible for this subject: it is a consumer "
            "account, which has no Workspace admin console to grant it. Gmail and "
            "Admin Reports are therefore unreachable. Lifting this needs a subject on "
            "a Workspace domain plus authorisation of the service account's client_id "
            "for the connector's scopes."
        ),
        subject_domain=live_config.gws_subject_user.split("@")[-1],
        client_id_needing_authorisation=live_config.gws_client_id,
    )

    assert not live_config.gws_can_impersonate()


def test_a_shared_folder_is_readable(live_config: LiveConfig, folder_id: str) -> None:
    names = effects.drive_file_names(live_config, folder_id=folder_id)

    record(
        "google_workspace",
        Control.UPSTREAM,
        Verdict.PASS,
        detail="the shared Drive folder is listable by the service account",
        folder=live_config.gws_drive_folder_name,
        files_visible=len(names),
    )
    assert isinstance(names, list)


def test_the_service_account_cannot_seed_its_own_fixtures(
    live_config: LiveConfig, folder_id: str, created_files: list[str]
) -> None:
    """A service account has no storage quota, so it cannot create Drive files.

    The folder grants `canAddChildren`, which makes this look possible right up
    until the upload, when Drive returns 403 `storageQuotaExceeded`: a created
    file would be *owned* by the service account, and a service account owns no
    storage. Shared Drives solve this because files there are owned by the
    drive, but this tenant has none.

    Asserted rather than assumed, because it decides how every Drive fixture
    has to be arranged: a human places the file, and the service account only
    reads it.
    """
    from googleapiclient.errors import HttpError

    name = f"interlock-live-cert-{live_config.run_id}.txt"
    body = "certification fixture\nssn=123-45-6789\n"

    try:
        file_id = effects.drive_create_text_file(live_config, folder_id, name, body)
    except HttpError as exc:
        record(
            "google_workspace",
            Control.UPSTREAM,
            Verdict.NOT_APPLICABLE,
            detail=(
                "the service account cannot create Drive files: it owns no storage "
                "quota, so uploads fail with storageQuotaExceeded even in a folder "
                "granting canAddChildren. Fixtures must therefore be placed by a "
                "human, or the tenant must provide a Shared Drive."
            ),
            status=exc.resp.status,
            reason="storageQuotaExceeded",
        )
        assert exc.resp.status == 403
        return

    # If this tenant ever gains a Shared Drive, creation starts working and
    # the certification should say so rather than silently keep the old note.
    created_files.append(file_id)
    fetched = effects.drive_file_content(live_config, file_id)
    record(
        "google_workspace",
        Control.UPSTREAM,
        Verdict.PASS if fetched == body else Verdict.FAIL,
        detail="the service account created and read back a Drive file",
        round_tripped=fetched == body,
    )
    assert fetched == body


def test_a_document_placed_by_a_human_is_readable(live_config: LiveConfig, folder_id: str) -> None:
    """The read path the governed redaction test depends on.

    Skips, with instructions, when the folder is empty. A skip states what is
    missing; a pass on an empty folder would state nothing at all.
    """
    names = effects.drive_file_names(live_config, folder_id=folder_id)
    if not names:
        pytest.skip(
            f"the shared folder {live_config.gws_drive_folder_name!r} is empty. "
            "Place a plain-text or Google Doc file in it containing a synthetic "
            "SSN such as 123-45-6789 so Drive redaction can be certified."
        )

    record(
        "google_workspace",
        Control.UPSTREAM,
        Verdict.PASS,
        detail="a human-placed document in the shared folder is readable",
        files_visible=len(names),
    )
    assert names


def test_interlock_refuses_to_write_to_google_workspace(live_config: LiveConfig) -> None:
    """Pinned for the same reason as the Slack equivalent.

    The credential can create Drive files; the product will not. If writes are
    implemented later this fails and forces the certification matrix to be
    updated rather than continuing to claim the control is not applicable.
    """
    import asyncio

    from interlock.connections.connectors import get_adapter

    adapter = get_adapter("google_workspace", {"connector_key": "google_workspace"})

    with pytest.raises(NotImplementedError, match="approval-gated"):
        asyncio.run(
            adapter.execute_write(
                {
                    "source_id": "live_cert_google_workspace",
                    "identity_id": None,
                    "connection_config": {"workspace_domain": "certification"},
                    "query": "create a document",
                }
            )
        )

    record(
        "google_workspace",
        Control.WRITE_SAFETY,
        Verdict.NOT_APPLICABLE,
        detail=(
            "Google Workspace writes are disabled by design in the adapter; refusal "
            "proven rather than assumed. Write safety cannot be certified for a "
            "connector that cannot write."
        ),
        refusal="NotImplementedError",
    )
