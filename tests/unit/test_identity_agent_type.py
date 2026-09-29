"""A malformed identity row must not present as a bad credential.

`identities.agent_type` is parsed into the AgentType enum when an identity
authenticates. A value outside the enum raised while the identity context was
being built, and the failure reached the caller as 401 "Invalid API key" -
which sends an operator to re-issue a credential that was never wrong. It cost
several rounds of diagnosis against a key whose hash matched exactly.

Two changes, at both ends. The API rejects an unrecognised value, so it can no
longer create an identity that cannot authenticate; and the read path tolerates
one already stored, logging what is wrong instead of failing. `agent_type` is
descriptive - no authorization decision reads it - so failing an otherwise
valid identity over it was never the right trade.

"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from interlock.admin.routes.identities import IdentityCreate, IdentityUpdate
from interlock.core.auth import _agent_type_or_default
from interlock.models import AgentType


class TestWritePath:
    @pytest.mark.parametrize("value", [member.value for member in AgentType])
    def test_every_valid_agent_type_is_accepted(self, value: str) -> None:
        assert IdentityCreate(name="n", api_key="k" * 24, agent_type=value).agent_type == value

    def test_an_unrecognised_agent_type_is_rejected_at_creation(self) -> None:
        """The root cause: the API used to accept any string."""
        with pytest.raises(ValidationError, match="agent_type must be one of"):
            IdentityCreate(name="n", api_key="k" * 24, agent_type="certification")

    def test_the_rejection_says_why_it_matters(self) -> None:
        """A validation message that only lists valid values teaches nothing."""
        with pytest.raises(ValidationError) as excinfo:
            IdentityCreate(name="n", api_key="k" * 24, agent_type="nonsense")

        assert "cannot authenticate" in str(excinfo.value)

    def test_updates_are_validated_too(self) -> None:
        """Otherwise an identity can be broken after it was created correctly."""
        with pytest.raises(ValidationError, match="agent_type must be one of"):
            IdentityUpdate(agent_type="nonsense")

    def test_an_omitted_agent_type_on_update_is_still_allowed(self) -> None:
        """None means "leave it alone", not "set it to an invalid value"."""
        assert IdentityUpdate(name="renamed").agent_type is None


class TestReadPath:
    def test_a_valid_value_is_returned_unchanged(self) -> None:
        assert _agent_type_or_default("codex", "agent") is AgentType.CODEX

    def test_an_unrecognised_value_falls_back_rather_than_raising(self) -> None:
        """The identity keeps working; only its label is wrong.

        Failing closed here would take an otherwise valid identity offline
        over a descriptive field that no authorization decision reads.
        """
        assert _agent_type_or_default("certification", "agent") is AgentType.CUSTOM

    def test_the_fallback_is_logged_with_enough_detail_to_fix_the_row(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Failing soft is only acceptable if it is also loud.

        The log has to name the identity and the offending value, or the row
        stays wrong forever and the next person diagnoses it from scratch.
        """
        with caplog.at_level(logging.WARNING, logger="interlock.core.auth"):
            _agent_type_or_default("certification", "live-cert-agent")

        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "live-cert-agent" in message
        assert "certification" in message
        assert "custom" in message

    @pytest.mark.parametrize("value", [None, "", 42, object()])
    def test_any_unusable_value_falls_back_without_raising(self, value: object) -> None:
        """A NULL column, an empty string, a wrong type - none may break auth."""
        assert _agent_type_or_default(value, "agent") is AgentType.CUSTOM


def test_agent_type_is_not_an_authorization_input() -> None:
    """The premise the soft fallback rests on, pinned.

    If agent_type ever starts gating access, falling back to `custom` would be
    a privilege decision made by a logging branch, and this file's whole
    approach would need revisiting.
    """
    import inspect

    from interlock.core import policy, source_roles
    from interlock.gateway import pipeline

    for module in (source_roles, policy, pipeline):
        assert "agent_type" not in inspect.getsource(module), (
            f"{module.__name__} now reads agent_type. If it affects an "
            "authorization decision, auth must stop silently defaulting it."
        )
