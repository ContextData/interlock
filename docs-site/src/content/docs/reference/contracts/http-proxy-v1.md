---
title: "HTTP Proxy V1 Contract"
description: "The HTTP proxy behaviour promised in V1."
normative: true
sidebar:
  order: 6
---

Status: stable release-candidate surface for `1.0.0-rc.1`.

## Endpoint And Authentication

```text
GET|POST|PUT|PATCH|DELETE /proxy/{source_id}/{path}
Authorization: Bearer <agent-api-key>
```

`source_id` identifies a registered and enabled HTTP source. `path` is
resolved beneath that source's configured base URL. V1 does not expose HEAD,
OPTIONS, CONNECT, or arbitrary absolute-URL proxying.

Every request is processed in this order:

1. Validate encoding, path, and bounded body.
2. Resolve identity and source.
3. Evaluate source-role permissions from method and path.
4. Evaluate policy and rate limit.
5. Classify write risk and queue approval when required.
6. Validate outbound egress and circuit state.
7. Read from governance-scoped cache when eligible.
8. Execute upstream request.
9. Redact response and cache the redacted representation when eligible.
10. Invalidate source dependencies after a successful mutation.
11. Emit canonical audit.

Policies cannot create source access. A source-role allow is required before
policy governance is evaluated.

## Path And Egress Rules

- The proxy rejects dot segments, encoded traversal, scheme injection,
  authority injection, and absolute URL smuggling.
- Redirects are not followed.
- Loopback, link-local, metadata, multicast, reserved, and private targets are
  blocked by default, including alternate numeric IP forms and DNS results.
- Private egress requires an explicit source configuration approved by an
  operator. That exception is source-scoped and audited.
- The configured base path is a containment boundary. A request cannot escape
  it with path normalization.

## Headers

InterLock does not transparently forward all headers.

Default request allowlist:

- `Accept`
- `Accept-Language`
- `Content-Type`
- `If-Match`
- `If-None-Match`
- `If-Modified-Since`
- `If-Unmodified-Since`
- `Idempotency-Key`

Default response allowlist:

- `Cache-Control`
- `Content-Type`
- `ETag`
- `Expires`
- `Last-Modified`
- `Vary`

Hop-by-hop headers, client `Authorization`, client cookies, proxy credentials,
forwarding headers, and upstream `Set-Cookie` are not forwarded. Upstream
credentials come only from the registered source configuration or secret
reference.

## Body And Response Behavior

- Request and response bodies are bounded by configured byte limits.
- JSON is recursively PII-scanned and policy-redacted.
- Text-like responses receive bounded text redaction where supported.
- Scanner failure fails closed by replacing the affected body or field.
- V1 buffers bounded upstream responses before returning them. Arbitrarily
  large pass-through streaming is not part of the V1 contract.
- Content encoding that cannot be validated safely is rejected.

## Cache Behavior

Only eligible successful GET responses are cached. The deterministic cache
scope includes source, identity, team, active-grant version, mapped upstream
role, source-role decision, policy/redaction decision, request path/query, and
source generation. A mutation advances the source consistency generation
before execution when strict write barriers are enabled.

InterLock never promises a cache hit. It promises that a cache hit is no less
restrictive than an origin response for the same governance scope.

## Mutation And Approval Behavior

POST, PUT, PATCH, and DELETE are writes. A source role must explicitly allow
the method/path. Policy and write classification may deny or queue the request;
risky mutations are approval-gated.

A queued response uses HTTP `202` and includes:

```json
{
  "error": "request queued for approval",
  "approval_id": 123,
  "approval_state": "pending"
}
```

Exact prose may vary. Consumers should use status and `approval_state`.
Approved writes execute exactly once using the original source and identity
scope. Rejected or expired approvals never execute.

## Stable Status Classes

| Status | Contract |
| ---: | --- |
| `200-299` | Upstream success or queued approval (`202`) |
| `400` | Invalid path, encoding, source configuration, or client input |
| `401` | Missing or invalid identity credential |
| `403` | Source-role/policy/egress denial |
| `404` | Unknown source |
| `413` | Request or response exceeds configured bound |
| `429` | Rate limit |
| `502` | Sanitized upstream request failure |
| `503` | Registry, circuit, cache barrier, or mandatory dependency unavailable |

Successful upstream status codes are preserved. Error bodies are sanitized and
must not expose source credentials, internal network details, or raw approval
payload secrets.
