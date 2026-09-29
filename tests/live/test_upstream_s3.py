"""Tier 0: the live S3 bucket, before InterLock is involved.

Object storage has no privilege matrix to establish, so this module proves the
round trip instead: an object put must be byte-identical when fetched, and an
object deleted must actually be gone. The delete half is the one that matters.
A delete the API reports as successful but never performs is the object
storage form of the Write Safety defect - the same shape as the approval that
was marked executed while the row survived upstream.

Every object is written under the run-stamped prefix and removed in teardown,
so a killed process leaves something the sweeper can find rather than
something anonymous.
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
        not load_live_config().has_s3(),
        reason="live S3 credentials are not configured",
    ),
]

_PAYLOAD = b"interlock live certification\nssn=123-45-6789\nemail=probe@example.com\n"


@pytest.fixture(scope="module", autouse=True)
def cleanup_run_prefix(live_config: LiveConfig) -> Iterator[None]:
    """Remove everything this run wrote, even if the module fails."""
    try:
        yield
    finally:
        effects.s3_delete_prefix(live_config, live_config.s3_run_prefix)


def test_the_bucket_is_listable(live_config: LiveConfig) -> None:
    """Listing must work before anything else is meaningful."""
    keys = effects.s3_object_keys(live_config, prefix=live_config.s3_prefix)

    record(
        "s3",
        Control.UPSTREAM,
        Verdict.PASS,
        detail="bucket listing succeeded",
        objects_under_configured_prefix=len(keys),
        bucket=live_config.s3_bucket,
        region=live_config.s3_region,
    )
    assert isinstance(keys, list)


def test_an_object_round_trips_byte_for_byte(live_config: LiveConfig) -> None:
    """A truncating or re-encoding store would corrupt governed reads silently."""
    key = f"{live_config.s3_run_prefix}roundtrip.txt"
    effects.s3_put(live_config, key, _PAYLOAD)

    client = effects._s3_client(live_config)
    fetched = client.get_object(Bucket=live_config.s3_bucket, Key=key)["Body"].read()

    record(
        "s3",
        Control.UPSTREAM,
        Verdict.PASS if fetched == _PAYLOAD else Verdict.FAIL,
        detail="object put and fetched back byte-identical",
        bytes_written=len(_PAYLOAD),
        bytes_read=len(fetched),
        identical=fetched == _PAYLOAD,
    )
    assert fetched == _PAYLOAD, "the object did not round trip byte for byte"


def test_a_deleted_object_is_really_gone(live_config: LiveConfig) -> None:
    """Proven by a HEAD from the independent reader, not by the delete's return.

    A delete call returning cleanly is exactly the kind of self-report the
    audit standard forbids concluding from.
    """
    key = f"{live_config.s3_run_prefix}deleteme.txt"
    effects.s3_put(live_config, key, _PAYLOAD)
    assert effects.object_exists(live_config, key), "precondition: the object was not created"

    effects.s3_delete_prefix(live_config, key)
    still_there = effects.object_exists(live_config, key)

    record(
        "s3",
        Control.UPSTREAM,
        Verdict.PASS if not still_there else Verdict.FAIL,
        detail="delete verified absent by a HEAD from the independent reader",
        present_after_delete=still_there,
    )
    assert not still_there, "the object survived a delete the API reported as successful"


def test_listing_is_scoped_to_the_run_prefix(live_config: LiveConfig) -> None:
    """Guards the isolation every other S3 test depends on.

    If the run prefix did not actually scope listings, teardown could delete
    somebody else's objects from a shared bucket.
    """
    key = f"{live_config.s3_run_prefix}scoped.txt"
    effects.s3_put(live_config, key, _PAYLOAD)

    scoped = effects.s3_object_keys(live_config, live_config.s3_run_prefix)

    record(
        "s3",
        Control.UPSTREAM,
        (
            Verdict.PASS
            if all(k.startswith(live_config.s3_run_prefix) for k in scoped)
            else Verdict.FAIL
        ),
        detail="prefix listing returns only this run's objects",
        keys_under_run_prefix=len(scoped),
    )
    assert scoped, "the run prefix listed nothing despite an object being written"
    assert all(
        k.startswith(live_config.s3_run_prefix) for k in scoped
    ), "prefix listing leaked objects from outside this run"
