"""Collectors for sources that are not SQL databases.

What these record is inventory: buckets and prefixes, channels, repositories,
objects and their fields, indices and collections. The gateway does not yet
check agent requests to these sources against it, which the console and
`interlock_describe_access` both say. Each collector reuses its adapter's own
connection code, so configuration validation, egress checks and secret
references apply exactly as they do to agent traffic, and each is capped and
reports what it could not finish.

Nothing reads content: no object bodies, message text, channel topics,
documents or points.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from interlock.catalog.collectors import CollectContext
from interlock.catalog.model import CatalogNode, CatalogSnapshot

# Per-collector caps. The node cap in ScanLimits still applies on top.
S3_MAX_DEPTH = 4
S3_MAX_PREFIXES = 5000
SLACK_MAX_CHANNELS = 5000
GITHUB_MAX_REPOS = 2000
SEARCH_MAX_INDICES = 500
SEARCH_MAX_FIELDS_PER_INDEX = 2000
QDRANT_MAX_COLLECTIONS = 500


def _snapshot(
    nodes: list[CatalogNode],
    *,
    limits_max_nodes: int,
    source_attributes: dict[str, Any],
    truncation: dict[str, Any],
    complete: list[tuple[str, ...]] | None = None,
) -> CatalogSnapshot:
    snapshot = CatalogSnapshot(source_attributes=source_attributes)
    if len(nodes) > limits_max_nodes:
        nodes = nodes[:limits_max_nodes]
        truncation = {**truncation, "max_nodes": limits_max_nodes}
    snapshot.nodes = nodes
    snapshot.truncation = truncation
    snapshot.truncated = bool(truncation)
    snapshot.complete_subtrees = [()] if not truncation else list(complete or [])
    return snapshot


# --------------------------------------------------------------------------
# Object storage: S3 and DigitalOcean Spaces
# --------------------------------------------------------------------------


def build_s3_snapshot(
    *,
    bucket: str,
    root_prefix: str,
    levels: dict[str, tuple[list[str], int]],
    max_nodes: int,
    truncation: dict[str, Any] | None = None,
) -> CatalogSnapshot:
    """The prefix tree from one-level listings, keyed by the prefix listed.

    Paths are the bucket and then one segment per prefix level below the
    configured prefix; resource keys are the `s3://bucket/prefix/` form role
    patterns use.
    """
    nodes = [
        CatalogNode(
            node_type="bucket",
            path=(bucket,),
            name=bucket,
            resource_key=f"s3://{bucket}/{root_prefix}",
            attributes={
                "prefix": root_prefix,
                "object_count": levels.get(root_prefix, ([], 0))[1],
            },
        )
    ]
    for listed, (children, _objects) in sorted(levels.items()):
        for child in children:
            relative = child[len(root_prefix) :].strip("/")
            segments = tuple(part for part in relative.split("/") if part)
            if not segments:
                continue
            nodes.append(
                CatalogNode(
                    node_type="prefix",
                    path=(bucket, *segments),
                    name=segments[-1],
                    resource_key=f"s3://{bucket}/{child}",
                    attributes={"object_count": levels.get(child, ([], 0))[1]},
                )
            )
    return _snapshot(
        nodes,
        limits_max_nodes=max_nodes,
        source_attributes={"bucket": bucket, "prefix": root_prefix},
        truncation=dict(truncation or {}),
        complete=[],
    )


async def collect_s3(ctx: CollectContext) -> CatalogSnapshot:
    from interlock.connections.connectors import _secret_value, get_adapter

    config = ctx.connection_config
    adapter = get_adapter(ctx.connector_key, {"connector_key": ctx.connector_key})
    adapter._validate_config(config)  # the adapter's URL and egress guards
    from interlock.connections.adapters.s3 import S3Adapter

    bucket = str(config["bucket"])
    root = str(config.get("prefix") or "")
    if root and not root.endswith("/"):
        root += "/"
    lister = S3Adapter(
        bucket=bucket,
        prefix=root,
        aws_access_key_id=_secret_value(config, "aws_access_key_id", "aws_access_key_id_ref"),
        aws_secret_access_key=_secret_value(
            config, "aws_secret_access_key", "aws_secret_access_key_ref"
        ),
        region_name=str(config.get("region_name") or "us-east-1"),
        endpoint_url=config.get("endpoint_url"),
    )
    await lister.initialize()
    if not lister.available:
        raise RuntimeError("aiobotocore is not installed")

    levels: dict[str, tuple[list[str], int]] = {}
    truncation: dict[str, Any] = {}
    frontier = [root]
    for depth in range(S3_MAX_DEPTH + 1):
        next_frontier: list[str] = []
        for prefix in frontier:
            children, objects = await lister.list_level(prefix)
            levels[prefix] = (children, objects)
            next_frontier.extend(children)
            if sum(len(c) for c, _ in levels.values()) >= S3_MAX_PREFIXES:
                truncation["max_prefixes"] = S3_MAX_PREFIXES
                break
        if truncation or not next_frontier:
            break
        if depth == S3_MAX_DEPTH:
            truncation["max_depth"] = S3_MAX_DEPTH
            break
        frontier = next_frontier
    return build_s3_snapshot(
        bucket=bucket,
        root_prefix=root,
        levels=levels,
        max_nodes=ctx.limits.max_nodes,
        truncation=truncation,
    )


# --------------------------------------------------------------------------
# Slack
# --------------------------------------------------------------------------


def build_slack_snapshot(
    *, workspace: str, channels: Iterable[dict[str, Any]], max_nodes: int, truncated: bool
) -> CatalogSnapshot:
    nodes: list[CatalogNode] = []
    for channel in channels:
        channel_id = str(channel.get("id") or "")
        if not channel_id:
            continue
        nodes.append(
            CatalogNode(
                node_type="channel",
                path=(channel_id,),
                name=str(channel.get("name") or channel_id),
                resource_key=f"slack://channel/{channel_id}",
                attributes={
                    "private": bool(channel.get("is_private")),
                    "archived": bool(channel.get("is_archived")),
                    "members": channel.get("num_members"),
                },
            )
        )
    return _snapshot(
        nodes,
        limits_max_nodes=max_nodes,
        source_attributes={"workspace": workspace},
        truncation={"max_channels": SLACK_MAX_CHANNELS} if truncated else {},
    )


async def collect_slack(ctx: CollectContext) -> CatalogSnapshot:
    from interlock.connections.connectors import _slack_client, get_adapter

    config = ctx.connection_config
    get_adapter("slack", {"connector_key": "slack"})._validate_config(config)
    client = _slack_client(config)
    types = config.get("channel_types") or ["public_channel"]
    if isinstance(types, str):
        types = [part.strip() for part in types.split(",") if part.strip()]
    channels: list[dict[str, Any]] = []
    cursor: str | None = None
    truncated = False
    while True:
        response = await client.conversations_list(
            types=",".join(types), exclude_archived=False, limit=200, cursor=cursor
        )
        channels.extend(dict(channel) for channel in response.get("channels") or [])
        if len(channels) >= SLACK_MAX_CHANNELS:
            channels = channels[:SLACK_MAX_CHANNELS]
            truncated = True
            break
        cursor = (response.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    return build_slack_snapshot(
        workspace=str(config.get("workspace") or ""),
        channels=channels,
        max_nodes=ctx.limits.max_nodes,
        truncated=truncated,
    )


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------


def build_github_snapshot(
    *, org: str, repos: Iterable[dict[str, Any]], max_nodes: int, truncated: bool
) -> CatalogSnapshot:
    nodes = [
        CatalogNode(
            node_type="repository",
            path=(org, str(repo["name"])),
            name=str(repo["name"]),
            resource_key=f"github://repo/{org}/{repo['name']}",
            attributes={
                "visibility": repo.get("visibility"),
                "archived": bool(repo.get("archived")),
                "default_branch": repo.get("default_branch"),
            },
        )
        for repo in repos
        if repo.get("name")
    ]
    return _snapshot(
        nodes,
        limits_max_nodes=max_nodes,
        source_attributes={"org": org},
        truncation={"max_repos": GITHUB_MAX_REPOS} if truncated else {},
    )


def _github_repos(adapter: Any, config: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    client = adapter._client(config)
    org = str(config["org"])
    names = config.get("repo_names") or config.get("repos")
    if isinstance(names, str):
        names = [part.strip() for part in names.split(",") if part.strip()]
    if names:
        repos = [client.get_repo(f"{org}/{name}") for name in names]
    else:
        repos = []
        for repo in client.get_organization(org).get_repos():
            repos.append(repo)
            if len(repos) > GITHUB_MAX_REPOS:
                break
    truncated = len(repos) > GITHUB_MAX_REPOS
    return (
        [
            {
                "name": repo.name,
                "visibility": getattr(repo, "visibility", None)
                or ("private" if repo.private else "public"),
                "archived": repo.archived,
                "default_branch": repo.default_branch,
            }
            for repo in repos[:GITHUB_MAX_REPOS]
        ],
        truncated,
    )


async def collect_github(ctx: CollectContext) -> CatalogSnapshot:
    from interlock.connections.connectors import get_adapter

    adapter = get_adapter("github", {"connector_key": "github"})
    adapter._validate_config(ctx.connection_config)
    repos, truncated = await asyncio.to_thread(_github_repos, adapter, ctx.connection_config)
    return build_github_snapshot(
        org=str(ctx.connection_config["org"]),
        repos=repos,
        max_nodes=ctx.limits.max_nodes,
        truncated=truncated,
    )


# --------------------------------------------------------------------------
# Salesforce
# --------------------------------------------------------------------------


def build_salesforce_snapshot(
    *, objects: Iterable[dict[str, Any]], max_nodes: int
) -> CatalogSnapshot:
    nodes: list[CatalogNode] = []
    for obj in objects:
        name = str(obj.get("name") or obj.get("object_type") or "")
        if not name:
            continue
        nodes.append(
            CatalogNode(
                node_type="object",
                path=(name,),
                name=name,
                resource_key=f"salesforce://{name}",
            )
        )
        for ordinal, field_info in enumerate(obj.get("fields") or [], start=1):
            field_name = str(field_info.get("name") or "")
            if field_name:
                nodes.append(
                    CatalogNode(
                        node_type="field",
                        path=(name, field_name),
                        name=field_name,
                        resource_key=f"salesforce://{name}/{field_name}",
                        ordinal=ordinal,
                        data_type=field_info.get("type"),
                    )
                )
    return _snapshot(
        nodes,
        limits_max_nodes=max_nodes,
        source_attributes={"scope": "configured objects"},
        truncation={},
    )


async def collect_salesforce(ctx: CollectContext) -> CatalogSnapshot:
    from interlock.connections.connectors import get_adapter

    adapter = get_adapter("salesforce", {"connector_key": "salesforce"})
    described = await adapter.introspect(ctx.connection_config)
    return build_salesforce_snapshot(
        objects=described.get("objects") or [], max_nodes=ctx.limits.max_nodes
    )


# --------------------------------------------------------------------------
# OpenSearch and Elasticsearch
# --------------------------------------------------------------------------


def flatten_mapping(properties: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """`(dotted field name, type)` for every leaf of an index mapping."""
    out: list[tuple[str, str]] = []
    for name, spec in (properties or {}).items():
        if not isinstance(spec, dict):
            continue
        full = f"{prefix}{name}"
        if isinstance(spec.get("properties"), dict):
            out.extend(flatten_mapping(spec["properties"], f"{full}."))
        else:
            out.append((full, str(spec.get("type") or "object")))
    return out


def build_search_snapshot(
    *,
    connector_key: str,
    indices: Iterable[dict[str, Any]],
    mappings: dict[str, list[tuple[str, str]]],
    max_nodes: int,
    truncated: bool,
) -> CatalogSnapshot:
    nodes: list[CatalogNode] = []
    for index in indices:
        name = str(index.get("index") or "")
        if not name or name.startswith("."):
            continue
        nodes.append(
            CatalogNode(
                node_type="index",
                path=(name,),
                name=name,
                resource_key=f"{connector_key}://{name}",
                attributes={"documents": index.get("docs.count"), "health": index.get("health")},
            )
        )
        fields = mappings.get(name, [])[:SEARCH_MAX_FIELDS_PER_INDEX]
        for ordinal, (field_name, field_type) in enumerate(fields, start=1):
            nodes.append(
                CatalogNode(
                    node_type="field",
                    path=(name, field_name),
                    name=field_name,
                    resource_key=f"{connector_key}://{name}/{field_name}",
                    ordinal=ordinal,
                    data_type=field_type,
                )
            )
    return _snapshot(
        nodes,
        limits_max_nodes=max_nodes,
        source_attributes={},
        truncation={"max_indices": SEARCH_MAX_INDICES} if truncated else {},
    )


async def collect_search(ctx: CollectContext) -> CatalogSnapshot:
    from interlock.connections.connectors import (
        _basic_auth,
        _connector_http_client,
        _join_url,
        _search_headers,
        get_adapter,
    )

    config = ctx.connection_config
    adapter = get_adapter(ctx.connector_key, {"connector_key": ctx.connector_key})
    indices = list((await adapter.introspect(config)).get("indices") or [])
    truncated = len(indices) > SEARCH_MAX_INDICES
    indices = indices[:SEARCH_MAX_INDICES]
    mappings: dict[str, list[tuple[str, str]]] = {}
    async with _connector_http_client(
        config, timeout=float(config.get("timeout_seconds") or 10)
    ) as client:
        for index in indices:
            name = str(index.get("index") or "")
            if not name or name.startswith("."):
                continue
            url = _join_url(
                str(config["base_url"]),
                f"/{quote(name, safe='')}/_mapping",
                config,
                adapter.definition,
            )
            resp = await client.get(url, headers=_search_headers(config), auth=_basic_auth(config))
            resp.raise_for_status()
            body: dict[str, Any] = resp.json() or {}
            entry: dict[str, Any] = body.get(name) or next(iter(body.values()), {})
            mapping: dict[str, Any] = entry.get("mappings") or {}
            mappings[name] = flatten_mapping(mapping.get("properties") or {})
    return build_search_snapshot(
        connector_key=ctx.connector_key,
        indices=indices,
        mappings=mappings,
        max_nodes=ctx.limits.max_nodes,
        truncated=truncated,
    )


# --------------------------------------------------------------------------
# Qdrant
# --------------------------------------------------------------------------


def build_qdrant_snapshot(
    *, collections: Iterable[dict[str, Any]], max_nodes: int, truncated: bool
) -> CatalogSnapshot:
    nodes: list[CatalogNode] = []
    for collection in collections:
        name = str(collection.get("collection") or "")
        if not name:
            continue
        nodes.append(
            CatalogNode(
                node_type="collection",
                path=(name,),
                name=name,
                resource_key=f"qdrant://{name}",
                attributes={
                    "points": collection.get("points_count"),
                    "status": collection.get("status"),
                },
            )
        )
        schema = collection.get("payload_schema") or {}
        for ordinal, (field_name, spec) in enumerate(sorted(schema.items()), start=1):
            nodes.append(
                CatalogNode(
                    node_type="field",
                    path=(name, str(field_name)),
                    name=str(field_name),
                    resource_key=f"qdrant://{name}/{field_name}",
                    ordinal=ordinal,
                    data_type=str((spec or {}).get("data_type") or "") or None,
                )
            )
    return _snapshot(
        nodes,
        limits_max_nodes=max_nodes,
        source_attributes={},
        truncation={"max_collections": QDRANT_MAX_COLLECTIONS} if truncated else {},
    )


async def collect_qdrant(ctx: CollectContext) -> CatalogSnapshot:
    from interlock.connections.connectors import get_adapter

    adapter = get_adapter("qdrant", {"connector_key": "qdrant"})
    collections = list((await adapter.introspect(ctx.connection_config)).get("collections") or [])
    truncated = len(collections) > QDRANT_MAX_COLLECTIONS
    return build_qdrant_snapshot(
        collections=collections[:QDRANT_MAX_COLLECTIONS],
        max_nodes=ctx.limits.max_nodes,
        truncated=truncated,
    )
