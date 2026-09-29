"""The non-SQL collectors' pure parts: what each listing becomes in the catalog."""

from __future__ import annotations

from interlock.catalog.collectors.services import (
    build_github_snapshot,
    build_qdrant_snapshot,
    build_s3_snapshot,
    build_salesforce_snapshot,
    build_search_snapshot,
    build_slack_snapshot,
    flatten_mapping,
)


def _by_path(snapshot):  # type: ignore[no-untyped-def]
    return {node.path: node for node in snapshot.nodes}


def test_s3_prefixes_become_a_tree_under_the_bucket() -> None:
    snapshot = build_s3_snapshot(
        bucket="docs",
        root_prefix="team/",
        levels={
            "team/": (["team/a/", "team/b/"], 3),
            "team/a/": (["team/a/x/"], 1),
            "team/b/": ([], 5),
            "team/a/x/": ([], 0),
        },
        max_nodes=100,
    )
    nodes = _by_path(snapshot)
    assert nodes[("docs",)].resource_key == "s3://docs/team/"
    assert nodes[("docs",)].attributes["object_count"] == 3
    assert nodes[("docs", "a", "x")].resource_key == "s3://docs/team/a/x/"
    assert nodes[("docs", "b")].attributes["object_count"] == 5
    assert snapshot.complete_subtrees == [()] and not snapshot.truncated


def test_a_capped_s3_listing_claims_no_completeness() -> None:
    snapshot = build_s3_snapshot(
        bucket="docs",
        root_prefix="",
        levels={"": (["a/"], 0)},
        max_nodes=100,
        truncation={"max_depth": 4},
    )
    assert snapshot.truncated and snapshot.complete_subtrees == []


def test_slack_channels_record_structure_not_content() -> None:
    snapshot = build_slack_snapshot(
        workspace="acme",
        channels=[
            {
                "id": "C1",
                "name": "general",
                "is_private": False,
                "num_members": 9,
                "topic": {"value": "secret plans"},
            },
            {"id": "", "name": "ignored"},
        ],
        max_nodes=100,
        truncated=False,
    )
    (channel,) = snapshot.nodes
    assert (channel.path, channel.name, channel.resource_key) == (
        ("C1",),
        "general",
        "slack://channel/C1",
    )
    assert "secret plans" not in repr(snapshot)


def test_github_repositories_are_keyed_by_org() -> None:
    snapshot = build_github_snapshot(
        org="acme",
        repos=[
            {"name": "api", "visibility": "private", "archived": False, "default_branch": "main"}
        ],
        max_nodes=100,
        truncated=True,
    )
    (repo,) = snapshot.nodes
    assert repo.resource_key == "github://repo/acme/api"
    assert snapshot.truncated and snapshot.truncation == {"max_repos": 2000}


def test_salesforce_objects_carry_their_fields() -> None:
    snapshot = build_salesforce_snapshot(
        objects=[{"name": "Account", "fields": [{"name": "Name", "type": "string"}]}],
        max_nodes=100,
    )
    nodes = _by_path(snapshot)
    assert nodes[("Account", "Name")].data_type == "string"
    assert nodes[("Account",)].resource_key == "salesforce://Account"


def test_search_mappings_flatten_to_dotted_fields_and_skip_system_indices() -> None:
    assert flatten_mapping(
        {"title": {"type": "text"}, "author": {"properties": {"email": {"type": "keyword"}}}}
    ) == [("title", "text"), ("author.email", "keyword")]
    snapshot = build_search_snapshot(
        connector_key="opensearch",
        indices=[{"index": "docs", "docs.count": "3"}, {"index": ".kibana"}],
        mappings={"docs": [("title", "text")]},
        max_nodes=100,
        truncated=False,
    )
    assert set(_by_path(snapshot)) == {("docs",), ("docs", "title")}


def test_qdrant_payload_schema_becomes_fields() -> None:
    snapshot = build_qdrant_snapshot(
        collections=[
            {
                "collection": "kb",
                "points_count": 4,
                "payload_schema": {"lang": {"data_type": "keyword"}},
            }
        ],
        max_nodes=100,
        truncated=False,
    )
    assert _by_path(snapshot)[("kb", "lang")].data_type == "keyword"


def test_the_node_cap_truncates_every_collector() -> None:
    snapshot = build_slack_snapshot(
        workspace="w",
        channels=[{"id": f"C{i}", "name": f"c{i}"} for i in range(5)],
        max_nodes=2,
        truncated=False,
    )
    assert len(snapshot.nodes) == 2 and snapshot.truncation == {"max_nodes": 2}
    assert snapshot.complete_subtrees == []
