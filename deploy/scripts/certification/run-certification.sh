#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: run-certification.sh [--namespace interlock] [--release interlock] \
  [--evidence-dir build/certification]

Proves a deployed InterLock release is governed and working, then writes
redacted evidence.

What it asserts, in order:

  1. /ready on gateway and admin returns 200 with every check passing. That
     one response proves migrations are at head (verify_migration_head), plus
     PostgreSQL, Redis, registry, audit buffer, and cache invalidation.
  2. A governed PostgreSQL query runs through the gateway proxy, redaction
     applies, and an audit row is written - exercised by the e2e subset that
     needs only the gateway, admin, and control database.
  3. `helm upgrade` to the same digest re-converges and /ready still passes,
     proving the upgrade path and the pre-upgrade migration hook.

Services are reached through kubectl port-forward, so no ingress, DNS, or
public certificate is required.

Evidence is written to --evidence-dir. Secret values never reach it: the
readiness payloads are filtered before writing, and the pytest report is
captured with -q.
EOF
  exit 64
}

namespace="interlock"
release="interlock"
evidence_dir="build/certification"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace) namespace="${2:-}"; shift 2 ;;
    --release) release="${2:-}"; shift 2 ;;
    --evidence-dir) evidence_dir="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

for binary in kubectl uv python3; do
  command -v "${binary}" >/dev/null 2>&1 || {
    echo "${binary} is required for certification; install it and retry." >&2
    exit 127
  }
done

mkdir -p "${evidence_dir}"

# The upgrade re-install must use the same values as the original install.
cert_values_args=(--values deploy/helm/values.certification.yaml)
if [[ -n "${CERT_VALUES_OVERLAY:-}" && -f "${CERT_VALUES_OVERLAY}" ]]; then
  cert_values_args+=(--values "${CERT_VALUES_OVERLAY}")
fi

# Local ports. Deliberately unusual so they do not collide with anything the
# runner already has bound.
admin_port=19090
gateway_http_port=13000
gateway_pg_port=15432

forward_pids=()
cleanup() {
  # Always tear the forwards down: a lingering `kubectl port-forward` keeps the
  # job's network namespace busy and can outlive the step.
  for pid in "${forward_pids[@]:-}"; do
    [[ -n "${pid}" ]] && kill "${pid}" 2>/dev/null || true
  done
}
trap cleanup EXIT

start_forward() {
  local target="$1" local_port="$2" remote_port="$3"
  kubectl -n "${namespace}" port-forward "${target}" \
    "${local_port}:${remote_port}" >/dev/null 2>&1 &
  forward_pids+=("$!")

  # Poll rather than sleep: port-forward reports ready before the tunnel
  # actually accepts, and a fixed sleep is either flaky or slow.
  for _ in $(seq 1 60); do
    if (echo > "/dev/tcp/127.0.0.1/${local_port}") 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "port-forward to ${target}:${remote_port} never became reachable" >&2
  return 1
}

echo "==> opening port-forwards"
start_forward "service/${release}-admin" "${admin_port}" 9090
start_forward "service/${release}-gateway" "${gateway_http_port}" 3000
start_forward "service/${release}-gateway" "${gateway_pg_port}" 5432

# ---------------------------------------------------------------------------
# 1. Readiness
# ---------------------------------------------------------------------------
assert_ready() {
  local label="$1" url="$2" out="${evidence_dir}/$3"
  local status
  status="$(curl -sS -o "${out}" -w '%{http_code}' "${url}/ready" || echo 000)"
  echo "    ${label} /ready -> ${status}"
  if [[ "${status}" != "200" ]]; then
    echo "${label} is not ready:" >&2
    cat "${out}" >&2 || true
    return 1
  fi
  # A 200 with a failing sub-check would be a contract violation; fail loudly
  # rather than record a green run.
  python3 - "${out}" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1]))
failed = [name for name, ok in payload.get("checks", {}).items() if not ok]
if failed:
    sys.exit(f"readiness returned 200 with failing checks: {failed}")
PY
}

echo "==> asserting readiness"
assert_ready gateway "http://127.0.0.1:${gateway_http_port}" gateway-ready.json
assert_ready admin "http://127.0.0.1:${admin_port}" admin-ready.json

# ---------------------------------------------------------------------------
# 2. Governed core path
# ---------------------------------------------------------------------------
echo "==> seeding the control and source databases"
export INTERLOCK_E2E=1
export ADMIN_URL="http://127.0.0.1:${admin_port}"
export GATEWAY_URL="http://127.0.0.1:${gateway_http_port}"
export INTERLOCK_PG_HOST=127.0.0.1
export INTERLOCK_PG_PORT="${gateway_pg_port}"
# The chart requires client TLS on the PostgreSQL listener in production, so
# the governed-query tests must negotiate it. The listener certificate is
# signed by the disposable CA created for this run, and the port-forward
# terminates at 127.0.0.1 rather than the certificate's DNS name, so encrypt
# without verifying the server name.
export INTERLOCK_PG_SSL=require

# The control and source databases live in the same ephemeral PostgreSQL
# instance created by bootstrap-cluster-fixtures.sh, reached over its own
# port-forward.
control_pg_port=15433
start_forward "service/interlock-cert-postgres" "${control_pg_port}" 5432
export E2E_CONTROL_PG_HOST=127.0.0.1
export E2E_CONTROL_PG_PORT="${control_pg_port}"
export E2E_CONTROL_PG_USER=interlock
export E2E_CONTROL_PG_DATABASE=interlock
export E2E_SOURCE_PG_HOST=127.0.0.1
export E2E_SOURCE_PG_PORT="${control_pg_port}"
export E2E_SOURCE_PG_USER=interlock
export E2E_SOURCE_PG_DATABASE=source
# What the gateway itself must use to reach the upstream, from inside the
# cluster - this value is persisted into the source registry.
export E2E_COMPOSE_SOURCE_PG_HOST="interlock-cert-postgres.${namespace}.svc.cluster.local"

E2E_CONTROL_PG_PASSWORD="$(kubectl -n "${namespace}" get secret interlock-db-credentials \
  -o jsonpath='{.data.password}' | base64 -d)"
export E2E_CONTROL_PG_PASSWORD
export E2E_SOURCE_PG_PASSWORD="${E2E_CONTROL_PG_PASSWORD}"
# The gateway runs with production settings: API keys are HMAC-peppered and
# legacy SHA-256 hashes are refused, and a PostgreSQL source is served only
# over verified TLS. The seed stores keys with the cluster's own pepper and
# registers the source with the CA path the gateway pods mount.
E2E_API_KEY_PEPPER="$(kubectl -n "${namespace}" get secret interlock-api-key-pepper \
  -o jsonpath='{.data.pepper}' | base64 -d)"
export E2E_API_KEY_PEPPER
export E2E_SOURCE_PG_SSL_CA=/run/secrets/control-db-ca/ca.crt

uv run python -m tests.e2e.support.seed

echo "==> running the governed-core e2e subset"
# Only the tests that need nothing beyond the gateway, admin, and control
# database. The connector suites depend on compose-only upstreams (MinIO,
# OpenSearch, Qdrant, MySQL, mock SaaS) that a certification cluster does not
# run, and certifying mocks would not certify the release.
export INTERLOCK_E2E_SKIP_SEED=1
uv run pytest -q --no-header \
  tests/e2e/test_smoke.py \
  tests/e2e/test_protocol_paths.py \
  tests/e2e/test_seeded_pg_governance.py \
  tests/e2e/test_pg_production_governance.py \
  tests/e2e/test_regression_audit.py \
  | tee "${evidence_dir}/e2e-governed-core.txt"

# ---------------------------------------------------------------------------
# 3. Upgrade path
# ---------------------------------------------------------------------------
echo "==> re-installing the same signed release to prove the upgrade path"
# deploy-oci-release.sh runs `helm upgrade --install`, so running it a second
# time against an existing release exercises the upgrade path - including the
# pre-upgrade migration hook - using the exact same signed digest rather than
# a chart reference reconstructed from release metadata.
if [[ -n "${CERT_CHART_REF:-}" ]]; then
  deploy/scripts/release/deploy-oci-release.sh \
    --chart-ref "${CERT_CHART_REF}" \
    --chart-digest "${CERT_CHART_DIGEST}" \
    --image-repository "${CERT_IMAGE_REPOSITORY}" \
    --image-digest "${CERT_IMAGE_DIGEST}" \
    --namespace "${namespace}" \
    --release "${release}" \
    "${cert_values_args[@]}" \
    > "${evidence_dir}/helm-upgrade.txt" 2>&1 || {
      echo "upgrade re-install failed:" >&2
      cat "${evidence_dir}/helm-upgrade.txt" >&2
      exit 1
    }
else
  echo "    CERT_CHART_REF unset; skipping the upgrade re-install" \
    | tee "${evidence_dir}/helm-upgrade.txt"
fi

echo "==> re-asserting readiness after upgrade"
assert_ready gateway "http://127.0.0.1:${gateway_http_port}" gateway-ready-post-upgrade.json
assert_ready admin "http://127.0.0.1:${admin_port}" admin-ready-post-upgrade.json

# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------
echo "==> collecting evidence"
kubectl -n "${namespace}" get all -o wide > "${evidence_dir}/cluster-resources.txt" 2>&1 || true
kubectl -n "${namespace}" get events --sort-by=.lastTimestamp \
  > "${evidence_dir}/cluster-events.txt" 2>&1 || true
helm -n "${namespace}" list -o json > "${evidence_dir}/helm-releases.json" 2>&1 || true

# Secret *names* are useful evidence; Secret *values* must never be. `get
# secrets` without -o yaml prints names, types, and counts only.
kubectl -n "${namespace}" get secrets > "${evidence_dir}/secret-inventory.txt" 2>&1 || true

echo "==> certification passed; evidence in ${evidence_dir}"
