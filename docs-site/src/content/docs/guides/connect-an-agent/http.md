---
title: HTTP APIs
description: Send an agent's HTTP requests to an API through the gateway's reverse proxy.
sidebar:
  order: 3
---

For a source on the `generic_rest` connector, the gateway is a reverse proxy.
Send the agent's requests to:

```text
$GATEWAY/proxy/{source_id}/{path}
Authorization: Bearer $API_KEY
```

The gateway strips the agent's `Authorization` header, applies source roles by
method and path (`http.get`, `http.post`, `http.delete` and so on), applies
policy, forwards the request to the source's `base_url` with the source's own
credentials, redacts the response, and audits it.

- `GET` and `HEAD` are reads and can be cached.
- `POST` is a low-risk write and runs; `PUT` and `PATCH` are medium and
  `DELETE` high, so they are queued for approval and answered `202` with the
  approval id.
- Request and response bodies are size-limited; oversized ones are refused.
- Private and cloud-metadata addresses are refused unless the source allows
  private egress.

See the [HTTP proxy contract](/reference/contracts/http-proxy-v1/).
