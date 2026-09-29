#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: bootstrap-cluster-fixtures.sh [--namespace interlock] [--timeout 300s]

Creates the ephemeral data tier and TLS material a disposable certification
cluster needs before `helm install`:

  - a PostgreSQL StatefulSet with TLS enabled, hosting the `interlock`
    control database and a separate `source` database used as a governed
    upstream,
  - a Redis Deployment,
  - a self-signed CA, a PostgreSQL server certificate whose SAN matches the
    in-cluster Service DNS name, and a certificate for the gateway's own
    PostgreSQL listener,
  - the five Secrets the chart requires.

Everything it creates lives in one namespace and dies with the cluster.

This exists so certification can run against the real production
configuration - verify-full database TLS, strict audit durability, peppered
API keys - rather than relaxing the config gate to accommodate a test
environment. Certifying a weakened configuration would certify nothing.

Secrets are generated per run with `openssl rand` and are never echoed. Do not
point this at a cluster you care about: it is for disposable infrastructure.
EOF
  exit 64
}

namespace="interlock"
timeout="300s"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace) namespace="${2:-}"; shift 2 ;;
    --timeout) timeout="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

for binary in kubectl openssl; do
  command -v "${binary}" >/dev/null 2>&1 || {
    echo "${binary} is required for certification fixtures; install it and retry." >&2
    exit 127
  }
done

pg_service="interlock-cert-postgres"
redis_service="interlock-cert-redis"
pg_dns="${pg_service}.${namespace}.svc.cluster.local"

workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT
umask 077

echo "==> namespace ${namespace}"
kubectl create namespace "${namespace}" --dry-run=client -o yaml | kubectl apply -f -

# ---------------------------------------------------------------------------
# TLS material
#
# verify-full checks the hostname against the certificate, so the server cert
# must carry the in-cluster Service DNS name as a SAN. A self-signed CA is
# appropriate here precisely because the whole trust chain is disposable.
# ---------------------------------------------------------------------------
echo "==> generating CA and server certificates"
openssl req -x509 -newkey rsa:4096 -sha256 -days 2 -nodes \
  -keyout "${workdir}/ca.key" -out "${workdir}/ca.crt" \
  -subj "/CN=InterLock Certification CA" >/dev/null 2>&1

issue_cert() {
  local name="$1" cn="$2" san="$3"
  openssl req -newkey rsa:2048 -nodes \
    -keyout "${workdir}/${name}.key" -out "${workdir}/${name}.csr" \
    -subj "/CN=${cn}" >/dev/null 2>&1
  printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\n' "${san}" \
    > "${workdir}/${name}.ext"
  openssl x509 -req -in "${workdir}/${name}.csr" \
    -CA "${workdir}/ca.crt" -CAkey "${workdir}/ca.key" -CAcreateserial \
    -out "${workdir}/${name}.crt" -days 2 -sha256 \
    -extfile "${workdir}/${name}.ext" >/dev/null 2>&1
}

issue_cert postgres "${pg_dns}" "DNS:${pg_dns},DNS:${pg_service},DNS:localhost,IP:127.0.0.1"
# The gateway presents this to agents connecting on its PostgreSQL port. It is
# reached through port-forward during certification, hence the localhost SANs.
issue_cert gateway-pg "interlock-gateway.${namespace}.svc.cluster.local" \
  "DNS:interlock-gateway.${namespace}.svc.cluster.local,DNS:interlock-gateway,DNS:localhost,IP:127.0.0.1"

# ---------------------------------------------------------------------------
# Secrets. Values are generated here and never printed.
# ---------------------------------------------------------------------------
echo "==> creating Secrets"
db_password="$(openssl rand -base64 36 | tr -d '\n=+/' | cut -c1-32)"
admin_secret_key="$(openssl rand -base64 48 | tr -d '\n')"
api_key_pepper="$(openssl rand -base64 48 | tr -d '\n')"

apply_secret() {
  # Create only when absent. Overwriting on re-run looks idempotent but is
  # not: PostgreSQL fixes its superuser password at initdb and ignores
  # POSTGRES_PASSWORD on later starts, so rotating this Secret leaves the
  # database on the old password and every client fails with
  # "password authentication failed". The same applies to the CA - replacing
  # it while the server keeps its original certificate breaks verify-full.
  #
  # Re-running is therefore safe and additive; to rotate, delete the
  # namespace and start over. This is disposable infrastructure, so that is
  # the cheaper contract.
  local kind="$1" name="$2"
  if kubectl -n "${namespace}" get secret "${name}" >/dev/null 2>&1; then
    echo "    ${name} already exists; keeping it"
    return 0
  fi
  kubectl -n "${namespace}" create secret "$@" --dry-run=client -o yaml \
    | kubectl -n "${namespace}" apply -f - >/dev/null
  echo "    ${name} created"
}

apply_secret generic interlock-db-credentials \
  --from-literal=username=interlock \
  --from-literal=password="${db_password}"
apply_secret generic interlock-admin-secret \
  --from-literal=secret-key="${admin_secret_key}"
apply_secret generic interlock-api-key-pepper \
  --from-literal=pepper="${api_key_pepper}"
apply_secret generic interlock-control-db-ca \
  --from-file=ca.crt="${workdir}/ca.crt"
apply_secret tls interlock-pg-listener-tls \
  --cert="${workdir}/gateway-pg.crt" --key="${workdir}/gateway-pg.key"
# Consumed by the PostgreSQL StatefulSet below, not by the chart.
apply_secret tls interlock-cert-postgres-tls \
  --cert="${workdir}/postgres.crt" --key="${workdir}/postgres.key"

echo "==> deploying ephemeral PostgreSQL and Redis"
kubectl -n "${namespace}" apply -f - <<YAML
apiVersion: v1
kind: Service
metadata:
  name: ${pg_service}
  labels: {app: ${pg_service}}
spec:
  ports: [{port: 5432, targetPort: 5432, name: postgres}]
  selector: {app: ${pg_service}}
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: ${pg_service}
spec:
  serviceName: ${pg_service}
  replicas: 1
  selector: {matchLabels: {app: ${pg_service}}}
  template:
    metadata:
      labels: {app: ${pg_service}}
    spec:
      securityContext: {fsGroup: 999}
      containers:
        - name: postgres
          image: postgres:16
          args:
            - -c
            - ssl=on
            - -c
            - ssl_cert_file=/tls/tls.crt
            - -c
            - ssl_key_file=/tls/tls.key
          env:
            - name: POSTGRES_USER
              value: interlock
            - name: POSTGRES_DB
              value: interlock
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef: {name: interlock-db-credentials, key: password}
            # The gateway proxies to a real upstream database during
            # certification rather than to its own control database.
            - name: POSTGRES_INITDB_ARGS
              value: "--auth-host=scram-sha-256"
          ports: [{containerPort: 5432}]
          volumeMounts:
            - {name: tls, mountPath: /tls, readOnly: true}
            - {name: data, mountPath: /var/lib/postgresql/data}
          readinessProbe:
            exec: {command: [pg_isready, -U, interlock]}
            initialDelaySeconds: 5
            periodSeconds: 5
      volumes:
        - name: tls
          secret:
            secretName: interlock-cert-postgres-tls
            defaultMode: 0600
            items:
              - {key: tls.crt, path: tls.crt}
              - {key: tls.key, path: tls.key}
        - name: data
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: ${redis_service}
  labels: {app: ${redis_service}}
spec:
  ports: [{port: 6379, targetPort: 6379, name: redis}]
  selector: {app: ${redis_service}}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${redis_service}
spec:
  replicas: 1
  selector: {matchLabels: {app: ${redis_service}}}
  template:
    metadata:
      labels: {app: ${redis_service}}
    spec:
      containers:
        - name: redis
          image: redis:7
          ports: [{containerPort: 6379}]
          readinessProbe:
            exec: {command: [redis-cli, ping]}
            initialDelaySeconds: 3
            periodSeconds: 5
YAML

echo "==> waiting for the data tier"
kubectl -n "${namespace}" rollout status "statefulset/${pg_service}" --timeout="${timeout}"
kubectl -n "${namespace}" rollout status "deployment/${redis_service}" --timeout="${timeout}"

# The governed-query certification needs an upstream database distinct from
# the control database, so the gateway is proxying to something real.
#
# PostgreSQL has no CREATE DATABASE IF NOT EXISTS, and \gexec is a psql
# meta-command that is not interpreted when the statement arrives through -c.
# Test for the database first, then create it, so re-running is safe.
# `kubectl exec` goes from the API server to the node's kubelet, and on a
# freshly created managed cluster that path can refuse for the first minutes
# even after the pod is Ready. Retry it rather than fail the run on it.
pg_exec() {
  local attempt
  for attempt in $(seq 1 12); do
    if kubectl -n "${namespace}" exec "statefulset/${pg_service}" -- \
         psql -U interlock -d interlock -v ON_ERROR_STOP=1 "$@"; then
      return 0
    fi
    echo "    kubectl exec failed (attempt ${attempt}/12); retrying in 10s" >&2
    sleep 10
  done
  return 1
}

echo "==> creating the source database"
present="$(pg_exec -tAc "SELECT 1 FROM pg_database WHERE datname='source'")"
if [[ "${present}" == *1* ]]; then
  echo "    source database already present"
else
  pg_exec -c "CREATE DATABASE source" >/dev/null
  echo "    source database created"
fi

echo "==> fixtures ready in namespace ${namespace}"
echo "    control database: ${pg_dns}:5432/interlock"
echo "    source database:  ${pg_dns}:5432/source"
echo "    redis:            ${redis_service}.${namespace}.svc.cluster.local:6379"
