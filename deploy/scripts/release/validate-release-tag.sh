#!/usr/bin/env bash
set -euo pipefail

tag="${1:-}"
if [[ -z "${tag}" ]]; then
  printf 'usage: %s vMAJOR.MINOR.PATCH[-PRERELEASE]\n' "$0" >&2
  exit 64
fi

semver='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-([0-9A-Za-z-]+\.)*[0-9A-Za-z-]+)?$'
if [[ ! "${tag}" =~ ${semver} ]]; then
  printf 'release tag must be valid SemVer prefixed with v: %s\n' "${tag}" >&2
  exit 65
fi

version="${tag#v}"
printf '%s\n' "${version}"
