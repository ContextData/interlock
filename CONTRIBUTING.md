# Contributing

Thanks for considering a contribution. This project is still pre-public-beta, so security, governance correctness, and reproducibility matter more than feature breadth.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Getting Set Up

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/). Docker is only
needed for the end-to-end stack.

Most changes need nothing more than the `dev` extra. The unit suite is green
with it alone - tests that require an optional extra skip themselves rather
than fail:

```bash
uv sync --locked --extra dev
uv run pytest tests/unit -q
```

Install the heavier extras only when you are working on what they cover; `make
install` pulls the full set (PyTorch, spaCy, FAISS, cloud connectors) and is a
multi-gigabyte download:

| Working on | Install |
| --- | --- |
| Admin UI, policy, cache, audit, docs | `uv sync --locked --extra dev` |
| Deep PII scanning | add `--extra pii` |
| Discovery vectors, embeddings | add `--extra ml --extra vector` |
| A specific connector | add `--extra connectors-tier1` or `--extra connectors-tier2` |
| GitHub/GitLab connectors | add `--extra connectors-repo` (LGPL-3.0, also in `production`; see `NOTICE`) |
| Everything, as CI runs it | `make install` |

`make type` and `make check` type-check modules that import the optional
extras, so they need more than `dev`: use `make install`, or at least
`uv sync --locked --extra dev --extra ingestion`.

### Infrastructure Targets

`make check-infra` needs `terraform`, `actionlint`, and `helm`. Install a
terraform build that matches your machine's architecture: on Apple Silicon,
`terraform version` must report `on darwin_arm64`. An x86_64 build runs under
Rosetta and executes the AWS provider emulated, which makes `terraform
validate` appear to hang rather than fail.

The provider lock files cover `darwin_arm64`, `darwin_amd64`, and
`linux_amd64`, so `terraform init` should not modify them. If yours is
rewritten, you are on a platform we have not locked - say so in your PR rather
than committing the change.

## Development Expectations

- Keep changes scoped and covered by tests.
- Add failing tests before changing behavior when fixing a bug.
- Do not commit live credentials, private keys, generated logs with secrets, screenshots containing secrets, or local credential file paths.
- Keep connector capabilities honest: unsupported writes must fail closed and must not be exposed as enabled Admin actions.
- Update documentation when behavior, setup, public interfaces, or security posture changes.

## Local Checks

Run the narrowest relevant tests while developing, then run the broader gates before asking for review:

```bash
make lint          # ruff over src, tests, tools, scripts
make test-unit     # the fast suite; green with the dev extra alone
make check         # lint + types + unit + integration
make e2e           # compose stack up, seed, e2e suite, tear down (needs Docker)
```

Live-system tests are opt-in and must be skipped unless temporary credentials are explicitly provided.

Two things the test suite enforces that are easy to trip over:

- **Migrations are immutable.** `verify_migrations` checks each file's checksum
  against the digest recorded when it was applied, so editing a shipped
  migration - even a comment - breaks readiness on every existing database.
  Add a new migration instead.
- **Docs are checked.** Relative links in tracked markdown must resolve, and
  several guards assert exact strings in the CI workflow, Makefile, and
  contract docs. If you change one of those, update the guard in the same
  commit.

## Pull Requests

Every PR should include:

- Summary of behavior changed.
- Tests run.
- Security or data-governance impact.
- Any skipped live certifications and why.

Public launch blockers are tracked in `docs-site/src/content/docs/project/release-process.md`.
