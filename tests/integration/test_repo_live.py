"""Optional live GitHub/GitLab connector certification checks."""

from __future__ import annotations

import json
import os

import pytest

from interlock.connections.connectors import get_adapter

pytestmark = pytest.mark.live


def _github_config() -> dict[str, str]:
    return {
        "base_url": os.environ.get("GITHUB_BASE_URL", ""),
        "org": os.environ.get("GITHUB_ORG", ""),
        "owner": os.environ.get("GITHUB_OWNER", ""),
        "repo_names": os.environ.get("GITHUB_REPO_NAMES", ""),
        "token": os.environ.get("GITHUB_TOKEN", ""),
        "token_ref": os.environ.get("GITHUB_TOKEN_REF", ""),
        "ref": os.environ.get("GITHUB_REF", ""),
        "include_code_paths": os.environ.get("GITHUB_INCLUDE_CODE_PATHS", ""),
        "max_repos": os.environ.get("GITHUB_MAX_REPOS", "5"),
        "max_issues_per_repo": os.environ.get("GITHUB_MAX_ISSUES_PER_REPO", "5"),
        "max_prs_per_repo": os.environ.get("GITHUB_MAX_PRS_PER_REPO", "5"),
        "max_releases_per_repo": os.environ.get("GITHUB_MAX_RELEASES_PER_REPO", "2"),
        "max_workflows_per_repo": os.environ.get("GITHUB_MAX_WORKFLOWS_PER_REPO", "2"),
    }


def _gitlab_config() -> dict[str, str]:
    return {
        "base_url": os.environ.get("GITLAB_BASE_URL", ""),
        "group": os.environ.get("GITLAB_GROUP", ""),
        "project": os.environ.get("GITLAB_PROJECT", ""),
        "token": os.environ.get("GITLAB_TOKEN", ""),
        "token_ref": os.environ.get("GITLAB_TOKEN_REF", ""),
        "ref": os.environ.get("GITLAB_REF", ""),
        "include_code_paths": os.environ.get("GITLAB_INCLUDE_CODE_PATHS", ""),
        "max_projects": os.environ.get("GITLAB_MAX_PROJECTS", "5"),
        "max_issues_per_project": os.environ.get("GITLAB_MAX_ISSUES_PER_PROJECT", "5"),
        "max_mrs_per_project": os.environ.get("GITLAB_MAX_MRS_PER_PROJECT", "5"),
        "max_pipelines_per_project": os.environ.get("GITLAB_MAX_PIPELINES_PER_PROJECT", "2"),
    }


def _has_github_config() -> bool:
    cfg = _github_config()
    return bool(cfg["token"] or cfg["token_ref"])


def _has_gitlab_config() -> bool:
    cfg = _gitlab_config()
    return bool(cfg["token"] or cfg["token_ref"])


@pytest.mark.integration
@pytest.mark.skipif(
    not _has_github_config(),
    reason="Set GITHUB_TOKEN or GITHUB_TOKEN_REF to run live GitHub tests.",
)
@pytest.mark.asyncio
async def test_live_github_probe_introspect_and_list_assets() -> None:
    adapter = get_adapter("github", {"connector_key": "github"})
    cfg = _github_config()

    probe = await adapter.probe(cfg)
    assert probe.healthy is True, probe.error

    metadata = await adapter.introspect(cfg)
    assert metadata["connector"] == "github"
    assert {obj["object_type"] for obj in metadata["objects"]} >= {"repository", "file"}

    assets = await adapter.list_assets(cfg)
    assert isinstance(assets, list)
    if cfg["repo_names"]:
        expected = {
            f"github://repo/{cfg['org']}/{repo.strip()}"
            for repo in cfg["repo_names"].replace("\n", ",").split(",")
            if repo.strip()
        }
        actual = {asset["asset_path"] for asset in assets if asset["asset_type"] == "repository"}
        assert expected <= actual
    for asset in assets:
        assert asset["asset_path"].startswith("github://")
        assert asset["metadata"]["connector_key"] == "github"
    repo_asset = next(asset for asset in assets if asset["asset_type"] == "repository")
    payload = json.loads((await adapter.fetch_asset(cfg, repo_asset["asset_path"])).decode())
    assert payload["full_name"] in repo_asset["asset_path"]


@pytest.mark.integration
@pytest.mark.skipif(
    not _has_gitlab_config(),
    reason="Set GITLAB_TOKEN or GITLAB_TOKEN_REF to run live GitLab tests.",
)
@pytest.mark.asyncio
async def test_live_gitlab_probe_introspect_and_list_assets() -> None:
    adapter = get_adapter("gitlab", {"connector_key": "gitlab"})
    cfg = _gitlab_config()

    probe = await adapter.probe(cfg)
    assert probe.healthy is True, probe.error

    metadata = await adapter.introspect(cfg)
    assert metadata["connector"] == "gitlab"
    assert {obj["object_type"] for obj in metadata["objects"]} >= {"project", "file"}

    assets = await adapter.list_assets(cfg)
    assert isinstance(assets, list)
    for asset in assets:
        assert asset["asset_path"].startswith("gitlab://")
        assert asset["metadata"]["connector_key"] == "gitlab"
