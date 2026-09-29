#!/usr/bin/env bash
set -euo pipefail

repository="${1:-}"
image="${2:-}"
chart="${3:-}"
if [[ -z "${repository}" || -z "${image}" || -z "${chart}" ]]; then
  printf 'usage: %s OWNER/REPOSITORY IMAGE@sha256:... CHART@sha256:...\n' "$0" >&2
  exit 64
fi

digest_ref='@sha256:[0-9a-f]{64}$'
[[ "${image}" =~ ${digest_ref} ]] || exit 65
[[ "${chart}" =~ ${digest_ref} ]] || exit 65

issuer="https://token.actions.githubusercontent.com"
# Anchored, with the dots escaped, so the signature must come from exactly this
# repository's release workflow on a tag: a renamed or similarly named
# repository can never satisfy it.
escaped_repository="${repository//./\\.}"
identity="^https://github\\.com/${escaped_repository}/\\.github/workflows/release\\.yml@refs/tags/v[0-9][^/]*$"

cosign verify \
  --certificate-oidc-issuer "${issuer}" \
  --certificate-identity-regexp "${identity}" \
  "${image}"
cosign verify \
  --certificate-oidc-issuer "${issuer}" \
  --certificate-identity-regexp "${identity}" \
  "${chart}"
