# Security Policy

## Reporting a Vulnerability

Do not open a public issue, discussion or pull request for a vulnerability, a
credential exposure, or a way around InterLock's governance controls (source
roles, policy, redaction, write approval, audit).

Report it privately, by either route:

- **GitHub private vulnerability reporting**: the "Report a vulnerability"
  button on this repository's Security tab.
- **Email**: security@contextdata.ai.

Include what you can of:

- The affected component, endpoint or connector, and the version or image
  digest.
- Steps to reproduce.
- The impact you observed or expect.
- Whether credentials, PII, audit data or upstream systems are exposed.

Please do not include real credentials or personal data from a live system;
describe them instead.

## What to Expect

- An acknowledgement within 3 business days.
- An initial assessment, including whether we can reproduce it and how severe
  we consider it, within 10 business days.
- Updates as the fix progresses, and credit in the release notes unless you
  ask not to be named.

We ask that you give us a reasonable time to release a fix before any public
disclosure, and we will agree a disclosure date with you.

## Supported Versions

InterLock is in release candidates for 1.0.0. Only the latest release
candidate receives security fixes; upgrade to it before reporting, where you
can.

| Version | Supported |
| --- | --- |
| Latest `1.0.0-rc.N` | Yes |
| Earlier release candidates | No |

## Supply Chain Expectations

- Release images are built from `uv.lock` and must publish SBOM/CVE/license
  evidence before public-beta tagging.
- Production images must not bake raw config files, `.env` files, credentials,
  service-account JSON, or local credential paths.
- Public release notes must identify any accepted high/critical vulnerability,
  license, or provenance exception with an owner and expiration date.

## Credential Handling

- Never commit raw credentials, private keys, service account JSON, `.env` files, live URLs tied to credentials, or local credential paths.
- Live certification reports must refer to credentials by safe secret reference only.
- Admin, audit, approval, connector, and worker logs must redact secret-like fields before display or persistence.
