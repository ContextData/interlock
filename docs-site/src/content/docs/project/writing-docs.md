---
title: Writing docs
description: How this documentation site is built, what is generated, and the rules pages follow.
---

The site is [Starlight](https://starlight.astro.build/) in `docs-site/`. Pages
are Markdown (or MDX when a page needs a component) under
`docs-site/src/content/docs/`, grouped by the sidebar in `docs-site/sidebar.json`.

## Build it

```bash
make docs-build
```

That runs `npm ci` and `npm run build` with Node 24. The build fails on any
broken internal link. For a live preview, run `npm run dev` inside `docs-site/`.

## Where it is published

The site is served at `https://interlock.contextdata.dev` by DigitalOcean App
Platform, as a static site defined in `.do/docs-app.yaml`. App Platform builds
`docs-site/` from `main` on every push (`npm ci`, then `npm run build`) and
serves `dist/`; a build that fails, including on a broken internal link, is not
deployed and the previous version keeps serving. To change the hosting, edit the
spec and apply it with `doctl apps update <app-id> --spec .do/docs-app.yaml`.

Canonical URLs and the sitemap use `https://interlock.contextdata.dev`. To build
the site for another address, such as a fork or a preview, set `DOCS_SITE_URL`
at build time.

## Generated pages

Pages with `generated: true` in their front matter are written by
`tools/docs/generate.py` from the code they describe: configuration, the admin
API and its roles, MCP tools, connectors, role conditions, feature status and
make targets. Do not edit them. Change the code, or the description in the code,
then run:

```bash
make docs-generate
```

`make docs-check`, and the unit suite, fail when a generated page is stale.

## Rules every page follows

- A `title` and a one-sentence `description` in the front matter.
- In the sidebar: pages are picked up by directory, so put a new page in the
  section it belongs to.
- No em dashes; use a hyphen or a colon.
- Say what the code does. A limitation belongs next to the claim it limits.
- No private hostnames, internal paths, personal addresses or real credentials.
  Examples use `example.com` and placeholders.
- Pages marked `normative: true` (contracts, runbooks, the connector support
  matrix) state what the project promises. Tests pin their wording; change
  them together with the tests and a CHANGELOG entry.

## Screenshots

Screenshots are captured from a running stack by `make docs-screenshots`, which
reads the scenes in `docs-site/screenshots.yaml` and writes a light and a dark
image of each. `make docs-screenshots-stack` brings up the seeded stack, captures
and tears it down. Refresh them when a release changes the console.
