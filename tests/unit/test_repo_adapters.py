from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from interlock.connections.connectors import _safe_iter, get_adapter
from interlock.core.source_roles import SourceRoleEvaluator


class FakePool:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def fetch(self, *_args):
        return self.rows


def test_github_adapter_imports_current_pygithub_client() -> None:
    # Smoke-tests the real PyGithub import/constructor, so it only runs when
    # the optional connectors-repo extra is installed. Contributors on a
    # plain `uv sync --extra dev` environment skip it instead of failing.
    pytest.importorskip("github", reason="requires the connectors-repo extra")

    adapter = get_adapter("github", {"connector_key": "github"})
    client = adapter._client({"token": "test-token"})

    assert client.__class__.__name__ == "Github"


class Label:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeNotSet:
    """Stand-in for PyGithub's NotSet sentinel."""


_FAKE_NOT_SET = _FakeNotSet()


class Content:
    decoded_content = b"# Runbook\nKeep builds green."


class GitHubItem:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class FakeGitHubRepo:
    full_name = "acme/app"
    description = "Agent app"
    default_branch = "main"
    private = True
    updated_at = datetime(2026, 5, 1, tzinfo=UTC)

    def get_issues(self, state: str = "open"):
        return [
            GitHubItem(
                number=7,
                title="Fix auth",
                body="Auth issue",
                state=state,
                updated_at=datetime(2026, 5, 2, tzinfo=UTC),
                labels=[Label("bug")],
                pull_request=None,
            )
        ]

    def get_pulls(self, state: str = "open"):
        return [
            GitHubItem(
                number=8,
                title="Add connector",
                body="PR body",
                state=state,
                updated_at=datetime(2026, 5, 3, tzinfo=UTC),
                labels=[Label("connector")],
                head=GitHubItem(ref="feature/repo"),
            )
        ]

    def get_releases(self):
        return [
            GitHubItem(
                tag_name="v1.0.0",
                title="v1",
                body="release notes",
                updated_at=datetime(2026, 5, 4, tzinfo=UTC),
            )
        ]

    def get_workflows(self):
        return [GitHubItem(name="CI", path=".github/workflows/ci.yml")]

    def get_issue(self, number: int):
        return GitHubItem(
            number=number,
            title="Fix auth",
            body="Auth issue",
            state="open",
            labels=[Label("bug")],
        )

    def get_pull(self, number: int):
        return GitHubItem(
            number=number,
            title="Add connector",
            body="PR body",
            state="open",
            head=GitHubItem(ref="feature/repo"),
        )

    def get_release(self, tag: str):
        return GitHubItem(tag_name=tag, title="v1", body="release notes")

    def get_contents(self, path: str, ref: object = _FAKE_NOT_SET):
        # Mirrors PyGithub 2.x, whose `is_optional(v, type)` accepts the type
        # or its NotSet sentinel and rejects None. The previous fake defaulted
        # to None and accepted it, so the suite could not see that the adapter
        # was passing a value the real library refuses.
        assert ref is _FAKE_NOT_SET or isinstance(ref, str), ref
        return Content()


class FakeGitHub:
    def get_organization(self, _org: str):
        return self

    def get_repos(self):
        return [FakeGitHubRepo()]

    def get_repo(self, full_name: str):
        repo = FakeGitHubRepo()
        repo.full_name = full_name
        return repo


class EmptyBrokenPaginatedList:
    def __iter__(self):
        raise IndexError("empty paginated list")


def test_repo_safe_iter_handles_empty_pygithub_paginated_list() -> None:
    assert _safe_iter(lambda: EmptyBrokenPaginatedList(), 2) == []


def test_github_assets_include_pr_release_workflow_and_code(monkeypatch) -> None:
    adapter = get_adapter("github", {"connector_key": "github"})
    monkeypatch.setattr(adapter, "_client", lambda _config: FakeGitHub())

    assets = adapter._list_assets_sync(
        {
            "org": "acme",
            "include_code_paths": "README.md,docs/runbook.md",
        }
    )

    assert [asset["asset_path"] for asset in assets] == [
        "github://repo/acme/app",
        "github://issue/acme/app/7",
        "github://pr/acme/app/8",
        "github://release/acme/app/v1.0.0",
        "github://workflow/acme/app/CI",
        "github://file/acme/app/README.md",
        "github://file/acme/app/docs/runbook.md",
    ]
    issue = assets[1]
    assert issue["metadata"]["org"] == "acme"
    assert issue["metadata"]["repo"] == "app"
    assert issue["metadata"]["labels"] == ["bug"]
    file_asset = assets[-1]
    assert file_asset["metadata"]["path"] == "docs/runbook.md"
    assert file_asset["metadata"]["branch"] == "main"


def test_github_assets_honor_repo_name_allowlist(monkeypatch) -> None:
    adapter = get_adapter("github", {"connector_key": "github"})
    monkeypatch.setattr(adapter, "_client", lambda _config: FakeGitHub())

    assets = adapter._list_assets_sync(
        {
            "org": "acme",
            "repo_names": "app,api",
            "max_issues_per_repo": "0",
            "max_prs_per_repo": "0",
            "max_releases_per_repo": "0",
            "max_workflows_per_repo": "0",
        }
    )

    assert [asset["asset_path"] for asset in assets] == [
        "github://repo/acme/app",
        "github://repo/acme/api",
    ]


def test_github_fetch_file_and_pr(monkeypatch) -> None:
    adapter = get_adapter("github", {"connector_key": "github"})
    monkeypatch.setattr(adapter, "_client", lambda _config: FakeGitHub())

    file_payload = json.loads(
        adapter._fetch_asset_sync({"ref": "main"}, "github://file/acme/app/README.md").decode()
    )
    pr_payload = json.loads(adapter._fetch_asset_sync({}, "github://pr/acme/app/8").decode())

    assert file_payload["content"] == "# Runbook\nKeep builds green."
    assert pr_payload["head"] == "feature/repo"


def test_github_permission_request_extracts_repo_context() -> None:
    adapter = get_adapter("github", {"connector_key": "github"})

    request = adapter.build_permission_request(
        source_id="gh",
        identity_id=10,
        operation="code",
        metadata={"asset_ref": "github://file/acme/app/docs/runbook.md", "branch": "main"},
    )

    assert request.action == "repo.code.read"
    assert request.resources == ["github://file/acme/app/docs/runbook.md"]
    assert request.metadata["org"] == "acme"
    assert request.metadata["repo"] == "app"
    assert request.metadata["path"] == "docs/runbook.md"
    assert request.metadata["branch"] == "main"


class Manager:
    def __init__(self, values: list | None = None, by_id: dict | None = None) -> None:
        self.values = values or []
        self.by_id = by_id or {}

    def list(self, **_kwargs):
        return self.values

    def get(self, key):
        return self.by_id.get(str(key), self.by_id.get(key))


class FakeGitLabProject:
    id = 101
    path_with_namespace = "acme/app"
    description = "Agent app"
    default_branch = "main"
    visibility = "private"
    last_activity_at = "2026-05-01T00:00:00Z"

    def __init__(self) -> None:
        issue = GitHubItem(
            iid=7,
            title="Fix auth",
            description="Issue body",
            state="opened",
            updated_at="2026-05-02T00:00:00Z",
            labels=["bug"],
        )
        mr = GitHubItem(
            iid=8,
            title="Add connector",
            description="MR body",
            state="opened",
            updated_at="2026-05-03T00:00:00Z",
            source_branch="feature/repo",
            labels=["connector"],
        )
        pipeline = GitHubItem(id=9, status="success", ref="main", updated_at="2026-05-04T00:00:00Z")
        self.issues = Manager([issue], {"7": issue})
        self.mergerequests = Manager([mr], {"8": mr})
        self.pipelines = Manager([pipeline], {"9": pipeline})
        self.files = GitHubItem(raw=lambda **_kwargs: b"# GitLab Runbook")


class FakeGitLab:
    def __init__(self) -> None:
        project = FakeGitLabProject()
        self.projects = Manager([GitHubItem(id=101)], {"101": project})

    def auth(self) -> None:
        return None


def test_gitlab_assets_include_mrs_pipelines_and_code(monkeypatch) -> None:
    adapter = get_adapter("gitlab", {"connector_key": "gitlab"})
    monkeypatch.setattr(adapter, "_client", lambda _config: FakeGitLab())

    assets = adapter._list_assets_sync({"include_code_paths": "README.md"})

    assert [asset["asset_path"] for asset in assets] == [
        "gitlab://project/101",
        "gitlab://issue/101/7",
        "gitlab://merge_request/101/8",
        "gitlab://pipeline/101/9",
        "gitlab://file/101/README.md",
    ]
    assert assets[2]["metadata"]["branch"] == "feature/repo"
    assert assets[-1]["metadata"]["path"] == "README.md"


def test_gitlab_fetch_file_and_merge_request(monkeypatch) -> None:
    adapter = get_adapter("gitlab", {"connector_key": "gitlab"})
    monkeypatch.setattr(adapter, "_client", lambda _config: FakeGitLab())

    file_payload = adapter._fetch_asset_sync({"ref": "main"}, "gitlab://file/101/README.md")
    mr_payload = json.loads(adapter._fetch_asset_sync({}, "gitlab://merge_request/101/8").decode())

    assert file_payload == b"# GitLab Runbook"
    assert mr_payload["source_branch"] == "feature/repo"


@pytest.mark.asyncio
async def test_repo_role_constraints_match_branch_path_and_labels() -> None:
    evaluator = SourceRoleEvaluator(
        FakePool(
            [
                {
                    "permission_id": 1,
                    "role_id": 1,
                    "role_key": "code_reader",
                    "effect": "allow",
                    "action": "repo.code.read",
                    "resource_type": "repo.path",
                    "resource_pattern": "*",
                    "constraints": {
                        "orgs": ["acme"],
                        "repos": ["app"],
                        "branches": ["main"],
                        "path_prefixes": ["docs/"],
                    },
                },
                {
                    "permission_id": 2,
                    "role_id": 2,
                    "role_key": "triage_operator",
                    "effect": "allow",
                    "action": "repo.action.execute",
                    "resource_type": "repo.issue",
                    "resource_pattern": "*",
                    "constraints": {"labels": ["bug"]},
                },
            ]
        )
    )
    adapter = get_adapter("github", {"connector_key": "github"})

    code_allowed = await evaluator.evaluate(
        adapter.build_permission_request(
            source_id="gh",
            identity_id=10,
            operation="code",
            metadata={"asset_ref": "github://file/acme/app/docs/runbook.md", "branch": "main"},
        )
    )
    code_denied = await evaluator.evaluate(
        adapter.build_permission_request(
            source_id="gh",
            identity_id=10,
            operation="code",
            metadata={"asset_ref": "github://file/acme/app/src/app.py", "branch": "main"},
        )
    )
    issue_action_allowed = await evaluator.evaluate(
        adapter.build_permission_request(
            source_id="gh",
            identity_id=10,
            operation="comment",
            metadata={"asset_ref": "github://issue/acme/app/7", "labels": ["bug"]},
        )
    )

    assert code_allowed.allowed is True
    assert code_denied.allowed is False
    assert issue_action_allowed.allowed is True


class _GithubApiError(Exception):
    """Stands in for github.GithubException, which is not importable here."""


class LazyForbiddenPaginatedList:
    """A PyGithub-shaped listing: the request happens on iteration, not on call.

    This is the distinction that matters. PyGithub's `get_*` methods return a
    PaginatedList immediately and only issue HTTP when it is walked, so a 403
    surfaces from `__iter__` - never from the call that produced it.
    """

    def __iter__(self):
        raise _GithubApiError("403 Resource not accessible by personal access token")


class LazyPartialPaginatedList:
    """Yields some rows, then fails - a page boundary the token cannot read."""

    def __iter__(self):
        yield GitHubItem(number=1, title="first", body="", state="open")
        yield GitHubItem(number=2, title="second", body="", state="open")
        raise _GithubApiError("403 on the second page")


def test_safe_iter_absorbs_a_failure_raised_while_iterating() -> None:
    """A listing the token may not read must not fail the whole sync.

    `_safe_iter` exists to make optional listings non-fatal, but it guarded
    only the eager call. Against a least-privilege token - one without
    `actions: read`, say - the 403 escaped from the iteration and aborted an
    otherwise healthy ingest.
    """
    assert _safe_iter(lambda: LazyForbiddenPaginatedList(), 5) == []


def test_safe_iter_keeps_what_it_read_before_the_failure() -> None:
    kept = _safe_iter(lambda: LazyPartialPaginatedList(), 5)

    assert [item.number for item in kept] == [1, 2]


def test_a_zero_limit_switches_a_listing_off(monkeypatch) -> None:
    """0 must mean none, not 'use the default'.

    It was read as `int(config.get(key) or default)`, so zero - being falsy -
    selected the default. There was therefore no way to turn a listing off,
    which is the obvious remedy when one of them is unreadable.
    """
    adapter = get_adapter("github", {"connector_key": "github"})
    monkeypatch.setattr(adapter, "_client", lambda _config: FakeGitHub())

    assets = adapter._list_assets_sync({"repo_names": ["acme/app"], "max_workflows_per_repo": 0})

    assert not [a for a in assets if a["asset_type"] == "workflow"]


def test_a_zero_limit_means_the_same_written_as_a_string(monkeypatch) -> None:
    """0 and "0" must agree - the old idiom made them differ."""
    adapter = get_adapter("github", {"connector_key": "github"})
    monkeypatch.setattr(adapter, "_client", lambda _config: FakeGitHub())

    as_int = adapter._list_assets_sync({"repo_names": ["acme/app"], "max_issues_per_repo": 0})
    as_str = adapter._list_assets_sync({"repo_names": ["acme/app"], "max_issues_per_repo": "0"})

    assert [a["asset_path"] for a in as_int] == [a["asset_path"] for a in as_str]
    assert not [a for a in as_int if a["asset_type"] == "issue"]


def test_file_contents_are_fetchable_without_an_explicit_ref(monkeypatch) -> None:
    """The default configuration must be able to read a file.

    `ref=connection_config.get("ref") or None` passed None whenever the
    operator had not pinned a ref - and PyGithub 2.x rejects None, so every
    file asset failed at materialisation with a bare `AssertionError: None`
    while the repository and its issues indexed normally.
    """
    adapter = get_adapter("github", {"connector_key": "github"})
    monkeypatch.setattr(adapter, "_client", lambda _config: FakeGitHub())

    payload = json.loads(adapter._fetch_asset_sync({}, "github://file/acme/app/README.md").decode())

    assert payload["content"] == "# Runbook\nKeep builds green."
