#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: deploy-oci-release.sh \
  --chart-ref oci://registry/namespace/interlock \
  --chart-digest sha256:... \
  --image-repository registry/namespace/interlock-runtime \
  --image-digest sha256:... \
  --values /path/to/production-values.yaml \
  [--release interlock] [--namespace interlock] [--timeout 15m]

The values file must reference pre-created Kubernetes Secrets and production
PostgreSQL/Redis endpoints. This script never creates or accepts credentials.
EOF
  exit 64
}

release="interlock"
namespace="interlock"
timeout="15m"
chart_ref=""
chart_digest=""
image_repository=""
image_digest=""
# --values may be repeated; helm applies them left to right, so a later file
# overrides an earlier one. The certification run uses this to layer a
# provider-specific overlay onto the shared certification values.
values_files=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release) release="${2:-}"; shift 2 ;;
    --namespace) namespace="${2:-}"; shift 2 ;;
    --timeout) timeout="${2:-}"; shift 2 ;;
    --chart-ref) chart_ref="${2:-}"; shift 2 ;;
    --chart-digest) chart_digest="${2:-}"; shift 2 ;;
    --image-repository) image_repository="${2:-}"; shift 2 ;;
    --image-digest) image_digest="${2:-}"; shift 2 ;;
    --values) values_files+=("${2:-}"); shift 2 ;;
    *) usage ;;
  esac
done

digest_pattern='^sha256:[0-9a-f]{64}$'
[[ "${chart_ref}" == oci://* ]] || usage
[[ "${chart_ref}" != *@* ]] || usage
[[ "${chart_ref##*/}" != *:* ]] || usage
[[ "${chart_digest}" =~ ${digest_pattern} ]] || usage
[[ "${image_repository}" != *://* ]] || usage
[[ "${image_repository}" != *@* ]] || usage
[[ "${image_repository##*/}" != *:* ]] || usage
[[ "${image_digest}" =~ ${digest_pattern} ]] || usage
[[ ${#values_files[@]} -gt 0 ]] || usage
for values_file in "${values_files[@]}"; do
  [[ -f "${values_file}" ]] || usage
done

values_args=()
for values_file in "${values_files[@]}"; do
  values_args+=(--values "${values_file}")
done

# Charts before 1.0.0-rc.5 put per-release labels (helm.sh/chart,
# app.kubernetes.io/version) on the Gateway's volumeClaimTemplates. That field is
# immutable, so upgrading such a deployment to any newer chart was refused and
# rolled back. Replace only the StatefulSet object: --cascade=orphan keeps the pod
# and the audit spool volume, which the recreated StatefulSet adopts because its
# selector is unchanged. Done only while the live claim template still carries
# such a label, so it runs once per deployment and never on a first install.
legacy_claim_labels="$(kubectl --namespace "${namespace}" get statefulset "${release}-gateway" \
  --ignore-not-found \
  -o 'jsonpath={.spec.volumeClaimTemplates[*].metadata.labels.helm\.sh/chart}{.spec.volumeClaimTemplates[*].metadata.labels.app\.kubernetes\.io/version}' \
  || true)"
if [[ -n "${legacy_claim_labels}" ]]; then
  echo "Replacing statefulset/${release}-gateway to drop per-release claim-template labels; its pod and volume are kept." >&2
  kubectl --namespace "${namespace}" delete statefulset "${release}-gateway" --cascade=orphan
fi

helm upgrade --install "${release}" "${chart_ref}@${chart_digest}" \
  --namespace "${namespace}" \
  --create-namespace \
  "${values_args[@]}" \
  --set-string image.repository=${image_repository} \
  --set-string image.digest=${image_digest} \
  --set-string image.tag= \
  --atomic \
  --wait \
  --timeout "${timeout}"

kubectl --namespace "${namespace}" rollout status statefulset/"${release}"-gateway --timeout="${timeout}"
kubectl --namespace "${namespace}" rollout status deployment/"${release}"-admin --timeout="${timeout}"
kubectl --namespace "${namespace}" rollout status deployment/"${release}"-worker --timeout="${timeout}"
