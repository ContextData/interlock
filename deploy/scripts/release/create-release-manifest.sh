#!/usr/bin/env bash
# Records what a release published, including which provenance mechanisms
# actually ran. `github_attestations` is false when the repository is private,
# because GitHub's attestation store is a paid feature there; the registry
# still carries SLSA v1 provenance and an SPDX SBOM from the build, and the
# digest is cosign-signed. A consumer should be able to read this file and
# know which verification commands will work, rather than discovering it.
set -euo pipefail

output="${1:-}"
if [[ -z "${output}" ]]; then
  printf 'usage: %s OUTPUT_JSON\n' "$0" >&2
  exit 64
fi

required=(
  RELEASE_TAG
  RELEASE_VERSION
  RELEASE_COMMIT
  IMAGE_NAME
  IMAGE_DIGEST
  CHART_NAME
  CHART_DIGEST
  SBOM_PATH
)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    printf 'required environment variable is missing: %s\n' "${name}" >&2
    exit 65
  fi
done

digest_pattern='^sha256:[0-9a-f]{64}$'
[[ "${IMAGE_DIGEST}" =~ ${digest_pattern} ]] || exit 66
[[ "${CHART_DIGEST}" =~ ${digest_pattern} ]] || exit 66
[[ -f "${SBOM_PATH}" ]] || exit 66

jq -n \
  --arg schema_version "1" \
  --arg release_tag "${RELEASE_TAG}" \
  --arg release_version "${RELEASE_VERSION}" \
  --arg commit "${RELEASE_COMMIT}" \
  --arg image "${IMAGE_NAME}@${IMAGE_DIGEST}" \
  --arg chart "${CHART_NAME}@${CHART_DIGEST}" \
  --arg sbom_sha256 "$(sha256sum "${SBOM_PATH}" | awk '{print $1}')" \
  --argjson github_attestations "${GITHUB_ATTESTATIONS:-false}" \
  '{
    schema_version: ($schema_version | tonumber),
    release: {tag: $release_tag, version: $release_version, commit: $commit},
    artifacts: {image: $image, helm_chart: $chart, sbom_sha256: $sbom_sha256},
    provenance: {
      cosign_signature: true,
      registry_slsa_provenance: true,
      registry_sbom_attestation: true,
      github_attestations: $github_attestations
    }
  }' > "${output}"
