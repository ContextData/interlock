---
title: Contributing
description: How to propose a change, what a pull request needs, and the rules the tests enforce.
sidebar:
  order: 1
---

Contributions are welcome. InterLock governs access to data, so correctness,
security and honest documentation matter more than breadth. By taking part you
agree to the [Code of Conduct](https://github.com/ContextData/interlock/blob/main/CODE_OF_CONDUCT.md).

## Workflow

1. Open an issue for anything larger than a small fix, so the approach can be
   agreed first. Report vulnerabilities privately instead; see the
   [security policy](/project/security-policy/).
2. Set up a [development environment](/project/development-environment/).
3. Write a failing test for a bug before fixing it.
4. Run the narrowest tests while working, then the gates in
   [Testing](/project/testing/) before asking for review.
5. Open a pull request describing what changed, the tests you ran, any effect
   on security or data governance, and any live certification you skipped.

## Rules the tests enforce

- **Migrations are immutable.** Their checksums are recorded when applied;
  editing a shipped migration, even a comment, breaks readiness on every
  existing database. Add a new one.
- **Connector capabilities are honest.** An unsupported write fails closed and
  is never offered as an action.
- **Docs follow the code.** Generated reference pages must be current, links
  must resolve, and several tests pin exact wording in contracts, workflows and
  the Makefile. Change them in the same commit. See [Writing docs](/project/writing-docs/).
- **No secrets.** No credentials, keys, local paths or screenshots containing
  either. `make secret-scan` runs in CI.
- **Pinned dependencies.** Python dependencies are locked in `uv.lock` and
  exported to `requirements-production.txt`; CI fails if they disagree. GitHub
  Actions are pinned to commits.

The canonical text is `CONTRIBUTING.md` in the repository.
