---
title: Identities and API keys
description: How agents are identified, how their keys are stored, rotated and revoked.
sidebar:
  order: 6
---

An **identity** is one agent, or one class of agent, as InterLock sees it. Every
request is made by an identity, and every audit row names one. Give each agent
deployment its own identity, so an audit row says which agent did something and
revoking one agent does not affect the others.

## API keys

An identity authenticates with an API key. When the console creates an identity
it generates a random key and **shows it once**; InterLock keeps only a hash.
Keys are hashed with HMAC-SHA256 under a server-side pepper
(`auth.api_key_pepper`), so a copy of the database is not enough to use a key.
Production requires the pepper and refuses the older plain SHA-256 hashes.

Changing the pepper invalidates every existing key: stored hashes cannot be
recomputed, so every agent must be re-keyed. Choose it once.

- **Rotate** a key from the identity's page. The old key stops working at once
  and its cached session is dropped.
- **Revoke** an identity by disabling it. The next request is refused: sessions
  are revalidated against the database on every request.
- A key supplied by an operator rather than generated must be at least 32
  characters of printable ASCII.

## Other ways to authenticate

- **PostgreSQL username and password.** An identity can have a dedicated
  PostgreSQL username and password, for clients that cannot put an API key in
  the password field.
- **OIDC.** With OpenID Connect enabled, an agent can present a JWT from your
  identity provider, mapped to an identity.

## What an identity carries

- A name, an agent type (`claude_code`, `codex`, `copilot`, `custom`) and an
  optional team, used in audit and usage views.
- **Grants**: the source roles it holds on each source. See
  [Grants](/concepts/grants/).
- Legacy role labels, used only by policy conditions. They grant nothing.

## Deleting an identity

Deleting an identity keeps its name in a tombstone, so the audit log still says
`name (#7, deleted)` rather than a bare number.
