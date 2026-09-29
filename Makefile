.PHONY: docs-generate docs-check docs-licenses docs-build docs-screenshots docs-screenshots-stack live-check live-up live-down live-seed live-teardown live-sweep test-live-upstream test-live-governed live-certify audit-mutations help install lint type type-v1 test test-unit test-integration test-e2e test-mcp-sdk browser-install test-browser check check-quick e2e-up e2e-seed e2e-down e2e-logs e2e load secret-scan security audit-cover requirements-production requirements-production-check sbom cve-scan license-report release-evidence docker-build helm-render terraform-validate actionlint check-infra final-boss-local final-boss clean

E2E_COMPOSE=docker compose -p interlock-e2e -f docker-compose.yml -f docker-compose.e2e.yml
IMAGE_NAME ?= interlock-runtime
IMAGE_TAG ?= local
PYTHON_BASE_IMAGE ?= python:3.12-slim
RELEASE_EVIDENCE_DIR ?= build/release-evidence

help:
	@echo "InterLock development targets"
	@echo "  install            uv sync (dev + all extras)"
	@echo "  lint               ruff check"
	@echo "  type               mypy --strict on hardened modules"
	@echo "  type-v1            V1 Gateway/Admin/worker/stable-connector baseline gate"
	@echo "  test-unit          pytest -m 'not integration and not e2e and not load'"
	@echo "  test-integration   local/mocked integration suite; explicitly excludes live credentials"
	@echo "  test-mcp-sdk       official MCP 2026 SDK wire compatibility"
	@echo "  test-e2e           pytest -m e2e against an already-running seeded E2E stack"
	@echo "  test-browser       Playwright Admin certification against seeded E2E stack"
	@echo "  browser-install    install the pinned Chromium browser for certification"
	@echo "  check              lint + type + unit + integration"
	@echo "  check-quick        lint + unit only"
	@echo "  e2e-up             build/start compose + E2E overlay"
	@echo "  e2e-seed           seed the running E2E stack"
	@echo "  e2e-down           tear down E2E stack and volumes"
	@echo "  e2e-logs           print E2E stack logs"
	@echo "  e2e                clean start + seed + full E2E suite + teardown"
	@echo "  load               load tests (compose-backed)"
	@echo "  secret-scan        scan tracked files for known sensitive live artifacts"
	@echo "  docs-generate      regenerate the docs site's reference pages from the code"
	@echo "  docs-check         fail if a generated docs page is stale"
	@echo "  docs-licenses      licence and source gate for the docs site's npm packages"
	@echo "  docs-build         build the docs site (needs Node 24)"
	@echo "  docs-screenshots   capture docs screenshots from a running stack"
	@echo "  docs-screenshots-stack  bring up the e2e stack, capture, tear down"
	@echo "  security           secret-scan + bandit + pip-audit"
	@echo "  audit-cover        verify_audit_coverage.py"
	@echo "  requirements-production regenerate hash-pinned production export"
	@echo "  sbom               generate CycloneDX production SBOM from uv.lock"
	@echo "  cve-scan           pip-audit production requirements into CycloneDX report"
	@echo "  license-report     licence inventory of the published image; fails on a licence outside the allowlist"
	@echo "  release-evidence   requirements check + SBOM + CVE + license evidence"
	@echo "  docker-build       build local InterLock runtime image"
	@echo "  terraform-validate offline terraform fmt/validate for both cloud roots"
	@echo "  actionlint         lint GitHub Actions workflows"
	@echo "  check-infra        terraform-validate + actionlint + helm-render"
	@echo "  helm-render        render Helm manifests with the local helm CLI"
	@echo "  final-boss-local   full local public-beta release gate, no live credentials"
	@echo "  final-boss         alias for final-boss-local"
	@echo ""
	@echo "Live certification (real upstream systems; never part of any gate)"
	@echo "  live-check         offline: parse credentials, verify the matrix pin"
	@echo "  live-up            compose stack with live credentials in the gateway"
	@echo "  live-seed          register the five live systems as governed sources"
	@echo "  test-live-upstream Tier 0: upstreams and privileges, no InterLock"
	@echo "  test-live-governed Tier 1: governance against the live upstreams"
	@echo "  live-certify       seed, run both tiers, write the report, tear down"
	@echo "  live-teardown      remove every live_cert_* control-plane object"
	@echo "  live-sweep         delete orphaned artifacts left by a killed run"

install:
	uv sync --locked --extra dev --extra pii --extra vector --extra ml --extra ingestion --extra otel --extra connectors-tier1 --extra connectors-tier2 --extra connectors-repo --extra mcp-certification

lint:
	uv run ruff check src tests tools scripts
	# CI also runs this; without it `make lint` can pass locally and fail there.
	uv run black --check src tests tools scripts

type:
	uv run mypy --strict src/interlock/core src/interlock/cache src/interlock/audit src/interlock/notifications src/interlock/catalog src/interlock/security/approval_redaction.py src/interlock/config.py src/interlock/errors.py
	$(MAKE) type-v1

type-v1:
	uv run python tools/typecheck_v1.py

test-unit:
	uv run pytest -m "not integration and not e2e and not load and not live" tests/unit -q

test-integration:
	INTEGRATION_TEST=1 uv run pytest -m "not live" tests/integration -q

test-mcp-sdk:
	uv sync --locked --extra dev --extra mcp-certification
	uv run pytest tests/integration/test_mcp_official_sdk.py -q

test-e2e:
	INTERLOCK_E2E=1 uv run pytest -m "e2e and not live" tests/e2e -q

test-browser:
	INTERLOCK_E2E=1 INTERLOCK_BROWSER=1 uv run pytest -m browser tests/browser -q

browser-install:
	uv run playwright install chromium

check-quick: lint test-unit

check: lint type test-unit test-integration

e2e-up:
	$(E2E_COMPOSE) up -d --build --wait postgres redis migration source-postgres source-mysql http-upstream enterprise-sources s3-upstream source-opensearch source-qdrant gateway admin worker-1 worker-2

e2e-seed:
	uv run python -m tests.e2e.support.seed

e2e-down:
	# --remove-orphans matters when a service is renamed or dropped: `down`
	# alone leaves the old container running, and the first person to upgrade
	# past the MinIO-to-s3-upstream swap otherwise hits "port is already
	# allocated" from a container compose no longer knows about.
	$(E2E_COMPOSE) down -v --remove-orphans

# --- Live certification ----------------------------------------------------
# These targets are the ONLY way live tests run. Nothing below is reachable
# from check, check-quick, test-*, e2e, final-boss-local or audit-mutations,
# and tests/unit/test_live_isolation.py fails the build if that ever changes.
# Pointing this repository at production systems must stay an explicit act.

live-check:
	uv run python -m tests.live.preflight
	uv run pytest tests/live/test_certification_matrix.py -q

live-up:
	uv run python -m tests.live.support.stack up

live-down:
	uv run python -m tests.live.support.stack down

live-seed:
	uv run python -m tests.live.support.seed
	uv run python -m tests.live.support.stack restart-gateway

live-teardown:
	uv run python -m tests.live.support.seed --teardown

live-sweep:
	uv run python -m tests.live.support.sweep

test-live-upstream:
	INTERLOCK_LIVE=1 uv run pytest -m live tests/live -k upstream -q

test-live-governed:
	INTERLOCK_LIVE=1 uv run pytest -m live tests/live -k "governed or discovery" -q

# The pytest calls are '-' prefixed so the report is written even when a
# control fails: a certification that only exists when everything passes is
# worthless. Teardown always runs. The exit status comes from the report's
# failing-control count, not from pytest.
live-certify: live-seed
	-INTERLOCK_LIVE=1 uv run pytest -m live tests/live -q
	$(MAKE) live-teardown
	$(MAKE) live-sweep
	uv run python -m tests.live.report

e2e-logs:
	$(E2E_COMPOSE) logs --tail=200 migration gateway admin worker-1 worker-2 source-postgres source-mysql http-upstream enterprise-sources s3-upstream source-opensearch source-qdrant

e2e:
	$(MAKE) e2e-down
	$(MAKE) e2e-up || ($(MAKE) e2e-logs; $(MAKE) e2e-down; exit 1)
	$(MAKE) e2e-seed
	$(MAKE) test-e2e || ($(MAKE) e2e-logs; $(MAKE) e2e-down; exit 1)
	$(MAKE) e2e-down

load:
	docker compose up -d --wait postgres redis gateway admin
	uv run pytest -m "load and not live" tests/load -q || (docker compose down -v; exit 1)
	docker compose down -v

docs-generate:
	uv run python tools/docs/generate.py --write

docs-check:
	uv run python tools/docs/generate.py --check

docs-licenses:
	uv run python tools/docs/check_npm_lockfile.py

docs-build:
	@command -v npm >/dev/null 2>&1 || { echo "npm (Node 24) is required for docs-build; install Node and retry."; exit 127; }
	cd docs-site && npm ci --no-audit --no-fund && npm run build

# Needs a running stack and TOUR_ADMIN_PASSWORD; see docs-site/screenshots.yaml.
docs-screenshots:
	uv run python -m tools.capture.console_tour --base-url $${DOCS_ADMIN_URL:-http://127.0.0.1:9090} --password-env TOUR_ADMIN_PASSWORD --manifest docs-site/screenshots.yaml --out docs-site/src/assets/screenshots --viewport-only

docs-screenshots-stack:
	$(MAKE) e2e-up
	$(MAKE) e2e-seed
	TOUR_ADMIN_PASSWORD=e2e-admin-password $(MAKE) docs-screenshots || ($(MAKE) e2e-down; exit 1)
	$(MAKE) e2e-down

secret-scan:
	uv run python -m interlock.security.secret_scan

security: secret-scan
	uv run bandit -r src/interlock -ll -iii
	uv run pip-audit

audit-cover:
	uv run python scripts/verify_audit_coverage.py

# Proves the suite detects the defects it claims to guard. A control that can
# be removed with every test still green is not covered, however many tests
# reference it - which is the condition that let the write-safety defect ship.
# Requires the compose stack (make e2e-up).
audit-mutations:
	uv run python tools/audit/mutate.py --all

requirements-production:
	uv export --locked --format requirements.txt --extra production --no-dev --no-emit-project --no-header --output-file requirements-production.txt

requirements-production-check:
	mkdir -p $(RELEASE_EVIDENCE_DIR)
	uv export --locked --format requirements.txt --extra production --no-dev --no-emit-project --no-header --output-file $(RELEASE_EVIDENCE_DIR)/requirements-production.txt
	diff -u requirements-production.txt $(RELEASE_EVIDENCE_DIR)/requirements-production.txt

sbom:
	mkdir -p $(RELEASE_EVIDENCE_DIR)
	uv export --locked --format cyclonedx1.5 --extra production --no-dev --output-file $(RELEASE_EVIDENCE_DIR)/interlock-runtime.sbom.cdx.json

cve-scan:
	mkdir -p $(RELEASE_EVIDENCE_DIR)
	uv run pip-audit -r requirements-production.txt --disable-pip --no-deps --format cyclonedx-json --output $(RELEASE_EVIDENCE_DIR)/interlock-runtime.pip-audit.cdx.json

license-report: sbom
	uv run python tools/license_report.py --out $(RELEASE_EVIDENCE_DIR)

release-evidence: requirements-production-check sbom cve-scan license-report

docker-build:
	docker build --build-arg PYTHON_BASE_IMAGE=$(PYTHON_BASE_IMAGE) -t $(IMAGE_NAME):$(IMAGE_TAG) .

helm-render:
	@command -v helm >/dev/null 2>&1 || { echo "helm CLI is required for helm-render/final-boss; install Helm and retry."; exit 127; }
	helm template interlock deploy/helm/interlock
	helm template interlock deploy/helm/interlock --values deploy/helm/values.certification.yaml

# Offline: -backend=false means no bucket, no credentials, and no network.
terraform-validate:
	@command -v terraform >/dev/null 2>&1 || { echo "terraform CLI is required for terraform-validate/final-boss; install Terraform >= 1.11 and retry."; exit 127; }
	terraform fmt -check -recursive deploy/terraform
	terraform -chdir=deploy/terraform/environments/aws init -backend=false
	terraform -chdir=deploy/terraform/environments/aws validate
	terraform -chdir=deploy/terraform/environments/digitalocean init -backend=false
	terraform -chdir=deploy/terraform/environments/digitalocean validate

actionlint:
	@command -v actionlint >/dev/null 2>&1 || { echo "actionlint is required for actionlint/final-boss; install actionlint (brew install actionlint) and retry."; exit 127; }
	actionlint .github/workflows/ci.yml .github/workflows/release.yml .github/workflows/release-foundations.yml .github/workflows/cloud-certification.yml

check-infra: terraform-validate actionlint helm-render

final-boss-local:
	uv sync --locked --extra dev --extra pii --extra vector --extra ml --extra ingestion --extra otel --extra connectors-tier1 --extra connectors-tier2 --extra connectors-repo --extra mcp-certification
	uv run black --check src tests tools scripts
	uv run ruff check src tests tools scripts
	$(MAKE) type
	$(MAKE) test-unit
	$(MAKE) test-integration
	uv run pytest tests/integration/test_mcp_official_sdk.py -q
	$(MAKE) audit-cover
	$(MAKE) docs-check
	$(MAKE) security
	$(MAKE) release-evidence
	uv run pytest tests/unit/test_helm_validation.py -q
	$(MAKE) docker-build
	$(MAKE) terraform-validate
	$(MAKE) actionlint
	$(MAKE) helm-render
	$(MAKE) e2e-down
	$(MAKE) e2e-up || ($(MAKE) e2e-logs; $(MAKE) e2e-down; exit 1)
	$(MAKE) e2e-seed
	$(MAKE) e2e-seed
	$(MAKE) browser-install
	$(MAKE) test-e2e || ($(MAKE) e2e-logs; $(MAKE) e2e-down; exit 1)
	INTERLOCK_E2E=1 uv run pytest tests/e2e/test_source_roles_enforcement.py tests/e2e/test_seeded_pg_governance.py tests/e2e/test_seeded_mysql_connector.py tests/e2e/test_seeded_http_governance.py tests/e2e/test_seeded_s3_connector.py tests/e2e/test_seeded_enterprise_connectors.py tests/e2e/test_seeded_admin_and_pipeline.py tests/e2e/test_seeded_admin_pages.py -q || ($(MAKE) e2e-logs; $(MAKE) e2e-down; exit 1)
	$(MAKE) test-browser || ($(MAKE) e2e-logs; $(MAKE) e2e-down; exit 1)
	$(MAKE) audit-mutations || ($(MAKE) e2e-logs; $(MAKE) e2e-down; exit 1)
	$(MAKE) e2e-down
	$(MAKE) load

final-boss: final-boss-local

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache
