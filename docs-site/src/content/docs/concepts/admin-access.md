---
title: Admin access
description: Who can use the console and admin API, with which roles, and how sign-in works.
sidebar:
  order: 7
---

The console and the admin API are one server with one set of accounts, separate
from agent identities. Every admin action is recorded in the admin audit log.

## Roles

| Role | Can |
|---|---|
| `owner`, `admin` | everything |
| `security_admin` | identities, keys and grants, and any change without a narrower rule |
| `source_admin` | sources, connectors, source roles, catalog, ingestion |
| `policy_admin` | policy rules |
| `approval_reviewer` | approve and reject queued writes |
| `auditor` | read the console, audit and usage |

An account can hold several roles. Which routes each role opens is listed in
[Admin roles](/reference/admin-roles/).

## The first admin

A fresh install creates one admin, `admin`, with the password `admin`. Signing
in with it opens only a change-password page: every other page redirects there
and the API answers `403 password_change_required` until a new password of at
least 12 characters is set. Set `INTERLOCK_ADMIN__BOOTSTRAP_PASSWORD` before the
first start to choose the first password instead; that one is not forced to
change.

Change the default before the console is reachable by anyone else. Every admin
can change their own password from the sidebar; doing so signs that admin out
everywhere else.

## Sessions

A sign-in creates a session cookie, valid for eight hours by default. Every
`POST`, `PUT`, `PATCH` and `DELETE` must also carry the session's CSRF token in
the `X-CSRF-Token` header, from `GET /auth/csrf`; the console does this for you.
Changing an admin's roles, disabling it, or changing its password revokes its
sessions on their next request.

## Single sign-on

With OIDC enabled, admins sign in through your identity provider and receive
roles from a map of group names to roles. Local password sign-in can stay
available to `owner` and `admin` accounts as a break-glass path.
