from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
CLOUD_WORKFLOW = ROOT / ".github" / "workflows" / "cloud-certification.yml"
FOUNDATIONS_WORKFLOW = ROOT / ".github" / "workflows" / "release-foundations.yml"
TERRAFORM_ROOT = ROOT / "deploy" / "terraform"
RELEASE_SCRIPTS = ROOT / "deploy" / "scripts" / "release"


def _workflow(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def _terraform_text() -> str:
    return "\n".join(
        path.read_text()
        for path in sorted(TERRAFORM_ROOT.rglob("*.tf"))
        if ".terraform" not in path.parts
    )


def test_release_workflow_is_tag_only_and_least_privilege() -> None:
    workflow = _workflow(RELEASE_WORKFLOW)

    assert workflow["on"] == {"push": {"tags": ["v*.*.*"]}}
    assert workflow["permissions"] == {
        "contents": "write",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }
    assert "pull_request" not in workflow["on"]
    assert "workflow_dispatch" not in workflow["on"]


def test_release_workflow_publishes_one_multi_service_image_by_digest() -> None:
    body = RELEASE_WORKFLOW.read_text()

    assert "linux/amd64,linux/arm64" in body
    assert "docker/build-push-action@" in body
    assert "# v7" in body
    assert "push: true" in body
    assert "sbom: true" in body
    assert "provenance: mode=max" in body
    assert "PYTHON_BASE_IMAGE: python:3.12-slim@sha256:" in body
    assert "PYTHON_BASE_IMAGE=${{ env.PYTHON_BASE_IMAGE }}" in body
    assert "interlock.gateway" in body
    assert "interlock.admin" in body
    assert "interlock.worker" in body
    assert "@${IMAGE_DIGEST}" in body
    assert "type=raw,value=latest" not in body
    assert ":latest" not in body


def test_release_workflow_attests_signs_and_packages_immutable_artifacts() -> None:
    body = RELEASE_WORKFLOW.read_text()

    assert "anchore/sbom-action@" in body
    assert "actions/attest@" in body
    assert "sbom-path:" in body
    assert "push-to-registry: true" in body
    assert "sigstore/cosign-installer@" in body
    assert "# v4.1.2" in body
    assert "cosign sign --yes" in body
    assert "helm package" in body
    assert "helm push" in body
    assert "oci://ghcr.io/${REGISTRY_OWNER}/charts" in body
    assert "release-manifest.json" in body


@pytest.mark.parametrize(
    "path",
    [
        RELEASE_WORKFLOW,
        CLOUD_WORKFLOW,
        FOUNDATIONS_WORKFLOW,
        ROOT / ".github" / "workflows" / "ci.yml",
    ],
)
def test_release_workflows_pin_every_action_to_a_commit(path: Path) -> None:
    uses = re.findall(r"^\s*-?\s*uses:\s*([^\s#]+)", path.read_text(), flags=re.MULTILINE)

    assert uses
    for action in uses:
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", action), action


def test_release_publish_job_waits_for_locked_preflight() -> None:
    workflow = _workflow(RELEASE_WORKFLOW)
    body = RELEASE_WORKFLOW.read_text()

    assert workflow["jobs"]["release"]["needs"] == "preflight"
    assert workflow["jobs"]["release"]["environment"] == "release"
    assert "uv sync --locked" in body
    assert "make check" in body
    assert "make security" in body
    assert "make release-evidence" in body
    assert "test_release_cloud_automation.py" in body


def test_release_foundations_ci_validates_terraform_helm_and_workflows() -> None:
    body = FOUNDATIONS_WORKFLOW.read_text()

    assert "terraform fmt -check -recursive deploy/terraform" in body
    assert body.count("terraform init -backend=false") == 2
    assert body.count("terraform validate") == 2
    assert "helm lint deploy/helm/interlock" in body
    assert "actionlint" in body
    assert "test_release_cloud_automation.py" in body


def test_cloud_workflow_actually_certifies_after_deploying() -> None:
    """Deploying is not certifying.

    Before this was added the workflow provisioned a cluster, installed the
    release, and tore it down without asserting anything beyond `rollout
    status` - a green run proved only that pods started. These steps are what
    make the run evidence, so guard them against quiet removal.
    """
    workflow = _workflow(CLOUD_WORKFLOW)

    for provider in ("aws", "digitalocean"):
        names = [step.get("name", "") for step in workflow["jobs"][provider]["steps"]]

        assert "Bootstrap ephemeral data tier and TLS material" in names, provider
        assert "Certify the governed core path" in names, provider
        assert "Upload certification evidence" in names, provider

        # Order matters: fixtures must exist before the chart is installed,
        # certification must run against the installed release, and evidence
        # must be uploaded before the environment is destroyed.
        order = {name: index for index, name in enumerate(names)}
        assert (
            order["Bootstrap ephemeral data tier and TLS material"]
            < order["Install exact signed release"]
        ), provider
        assert (
            order["Install exact signed release"] < order["Certify the governed core path"]
        ), provider
        assert (
            order["Certify the governed core path"] < order["Upload certification evidence"]
        ), provider
        assert order["Upload certification evidence"] < len(names) - 1, provider


def test_cloud_workflow_uploads_evidence_even_when_certification_fails() -> None:
    """A failed run is exactly when the evidence matters most."""
    workflow = _workflow(CLOUD_WORKFLOW)

    for provider in ("aws", "digitalocean"):
        upload = next(
            step
            for step in workflow["jobs"][provider]["steps"]
            if step.get("name") == "Upload certification evidence"
        )
        assert upload["if"] == "always()", provider


def test_certification_scripts_are_executable_and_syntactically_valid() -> None:
    scripts = sorted((ROOT / "deploy" / "scripts" / "certification").glob("*.sh"))

    assert scripts, "certification scripts are missing"
    for script in scripts:
        assert os.access(script, os.X_OK), script
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f"{script}: {result.stderr}"


def test_certification_runner_asserts_readiness_and_governed_queries() -> None:
    """The runner must prove governance, not just that the pods answer."""
    body = (ROOT / "deploy" / "scripts" / "certification" / "run-certification.sh").read_text()

    assert "/ready" in body
    assert "failing checks" in body, "a 200 with failing sub-checks must not pass"
    assert "test_seeded_pg_governance.py" in body, "governed query proof missing"
    assert "helm" in body and "upgrade" in body, "upgrade path proof missing"
    # Secret values must never reach the evidence directory.
    assert "-o yaml" not in body.split("get secrets")[-1][:80]


def test_cloud_workflow_is_manual_environment_gated_and_always_tears_down() -> None:
    workflow = _workflow(CLOUD_WORKFLOW)
    body = CLOUD_WORKFLOW.read_text()

    assert "workflow_dispatch" in workflow["on"]
    assert workflow["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "packages": "read",
    }
    assert "environment: cloud-certification" in body
    assert "if: always()" in body
    assert body.count(" destroy -auto-approve") == 2
    assert "secrets.AWS_DEPLOY_ROLE_ARN" in body
    assert "secrets.DIGITALOCEAN_TOKEN" in body
    assert "secrets.INTERLOCK_HELM_VALUES_B64" in body
    assert "password:" not in body.lower()


@pytest.mark.parametrize("provider", ["aws", "digitalocean"])
def test_cloud_roots_pin_providers_and_consume_release_digests(provider: str) -> None:
    root = TERRAFORM_ROOT / "environments" / provider
    body = "\n".join(path.read_text() for path in sorted(root.glob("*.tf")))

    # 1.11 is the floor for S3-backend native locking; DynamoDB locking was
    # removed in 1.13, so there is no version supporting both.
    assert 'required_version = ">= 1.11.0, < 2.0.0"' in body
    assert "helm_chart_ref" in body
    assert "helm_chart_digest" in body
    assert "image_repository" in body
    assert "image_digest" in body
    assert "release_artifacts" in body
    assert 'source = "../../modules/release-contract"' in body
    assert "token" not in body.lower()
    assert "password" not in body.lower()


@pytest.mark.parametrize("provider", ["aws", "digitalocean"])
def test_cloud_roots_keep_state_remote(provider: str) -> None:
    """Local state turns a cancelled run into permanently orphaned infra.

    `if: always()` runs the destroy step on cancellation, but GitHub's grace
    period is far shorter than an EKS teardown. When the runner is killed
    mid-destroy with local state, nothing can ever clean up the cluster, VPC,
    or load balancers again. A remote backend with a per-run key makes that
    recoverable.
    """
    root = TERRAFORM_ROOT / "environments" / provider
    body = "\n".join(path.read_text() for path in sorted(root.glob("*.tf")))

    assert 'backend "s3" {}' in body, "state must be remote, configured at init"

    # Backend credentials belong in the environment, never in tracked config.
    assert not re.search(r"(?i)(access_key|secret_key|api_token)\s*=", body)


def test_cloud_workflow_passes_per_run_state_keys() -> None:
    body = CLOUD_WORKFLOW.read_text()

    assert body.count("-backend-config=") >= 2
    # A shared key would let two runs collide and stomp each other's state.
    assert body.count("${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.tfstate") == 2
    assert "use_lockfile=true" in body, "AWS state needs locking"
    # Spaces is S3-compatible but not AWS; without these the backend fails.
    assert "skip_s3_checksum=true" in body
    assert "skip_credentials_validation=true" in body


def test_terraform_release_inputs_require_immutable_oci_digests() -> None:
    body = _terraform_text()

    assert (
        body.count("Artifact digests must use sha256 followed by 64 lowercase hex characters.") >= 2
    )
    assert body.count("Chart references must use the oci:// scheme.") >= 1
    assert "0.0.0.0/0" not in body
    assert not re.search(r"(?i)(access_key|secret_key|api_token)\s*=", body)


def test_cloud_modules_have_bounded_disposable_capacity() -> None:
    aws = "\n".join(
        path.read_text() for path in sorted((TERRAFORM_ROOT / "modules" / "aws-eks").glob("*.tf"))
    )
    digitalocean = "\n".join(
        path.read_text()
        for path in sorted((TERRAFORM_ROOT / "modules" / "digitalocean-doks").glob("*.tf"))
    )

    assert "single_nat_gateway = true" in aws
    assert "eks_managed_node_groups" in aws
    # Spot stays the default for disposable runs; a kept deployment passes
    # ON_DEMAND, since an interruption can strand a pod whose volume is zonal.
    assert "capacity_type  = var.node_capacity_type" in aws
    assert 'default     = "SPOT"' in aws
    assert "max_size       = var.max_nodes" in aws
    assert "auto_scale = true" in digitalocean
    assert "min_nodes  = var.min_nodes" in digitalocean
    assert "max_nodes  = var.max_nodes" in digitalocean


def test_aws_cluster_can_provision_the_audit_spool() -> None:
    """The gateway's audit spool is a PersistentVolumeClaim.

    EKS has no in-tree volume provisioner: without the EBS CSI driver the
    claim stays Pending, the install waits out its timeout, and the
    certification run fails before testing anything.
    """
    aws = "\n".join(
        path.read_text() for path in sorted((TERRAFORM_ROOT / "modules" / "aws-eks").glob("*.tf"))
    )

    assert "aws-ebs-csi-driver" in aws
    assert "pod_identity_association" in aws
    assert "AmazonEBSCSIDriverPolicy" in aws
    assert "defaultStorageClass" in aws
    # `disk_size` is ignored under the module's launch template.
    assert not re.search(r"^\s*disk_size\s*=", aws, re.M)
    assert "volume_size           = 50" in aws


def test_aws_managed_data_tier_is_opt_in_private_and_encrypted() -> None:
    root = "\n".join(
        path.read_text() for path in sorted((TERRAFORM_ROOT / "environments" / "aws").glob("*.tf"))
    )
    module = "\n".join(
        path.read_text()
        for path in sorted((TERRAFORM_ROOT / "modules" / "aws-data-tier").glob("*.tf"))
    )

    assert "count  = var.managed_data_tier ? 1 : 0" in root
    assert 'variable "managed_data_tier"' in root and "default     = false" in root
    assert "publicly_accessible    = false" in module
    assert "storage_encrypted = true" in module
    assert "manage_master_user_password = true" in module
    assert "transit_encryption_enabled = true" in module
    assert 'transit_encryption_mode    = "required"' in module
    assert "at_rest_encryption_enabled = true" in module
    # Reachable from the cluster's nodes only, never from an address range.
    assert module.count("referenced_security_group_id") == 2
    assert "cidr_ipv4" not in module
    assert "sensitive   = true" in module


LOCKED_TERRAFORM_PLATFORMS = ("darwin_arm64", "darwin_amd64", "linux_amd64")


@pytest.mark.parametrize("provider", ["aws", "digitalocean"])
def test_terraform_provider_lockfiles_cover_release_platforms(provider: str) -> None:
    """Every provider block must carry a package hash per locked platform.

    A file-wide count passes while an individual provider covers only the
    machine that last ran `init`, which is how contributors on an unlocked
    platform end up rewriting the lock on checkout. Counting per provider
    block catches that.

    This is a proxy, not proof: the lock format does not record which platform
    an `h1:` hash belongs to, so a block can hold three hashes for the wrong
    three platforms. The count is what is mechanically checkable here; the
    guarantee comes from re-running `terraform providers lock` with the full
    `-platform` set, which the failure message spells out.
    """
    lockfile = TERRAFORM_ROOT / "environments" / provider / ".terraform.lock.hcl"
    assert lockfile.exists()
    body = lockfile.read_text()

    per_provider: dict[str, int] = {}
    current: str | None = None
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("provider "):
            current = stripped.split('"')[1]
            per_provider.setdefault(current, 0)
        elif current and stripped.startswith('"h1:'):
            per_provider[current] += 1

    assert per_provider, f"no provider blocks found in {lockfile}"
    for name, count in per_provider.items():
        assert count >= len(LOCKED_TERRAFORM_PLATFORMS), (
            f"{name} in {provider} has {count} h1 hashes, expected at least "
            f"{len(LOCKED_TERRAFORM_PLATFORMS)} for {LOCKED_TERRAFORM_PLATFORMS}. "
            "Re-run: terraform providers lock "
            + " ".join(f"-platform={p}" for p in LOCKED_TERRAFORM_PLATFORMS)
        )

    assert '"zh:' in body
    assert "token" not in body.lower()
    assert "password" not in body.lower()


def test_deploy_script_requires_exact_chart_and_image_digests() -> None:
    body = (RELEASE_SCRIPTS / "deploy-oci-release.sh").read_text()

    assert "set -euo pipefail" in body
    assert "${chart_ref}@${chart_digest}" in body
    assert "--set-string image.repository=${image_repository}" in body
    assert "--set-string image.digest=${image_digest}" in body
    assert "--atomic" in body
    assert "--wait" in body
    assert '--values "${values_file}"' in body
    assert "--password" not in body


def test_release_shell_scripts_parse_cleanly() -> None:
    for path in sorted(RELEASE_SCRIPTS.glob("*.sh")):
        result = subprocess.run(
            ["bash", "-n", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{path}: {result.stderr}"


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("v1.0.0", "1.0.0"),
        ("v1.0.0-rc.1", "1.0.0-rc.1"),
        ("v12.34.56", "12.34.56"),
    ],
)
def test_release_tag_validation_accepts_semver(tag: str, expected: str) -> None:
    result = subprocess.run(
        [str(RELEASE_SCRIPTS / "validate-release-tag.sh"), tag],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    "tag",
    [
        "1.0.0",
        "v1",
        "v1.0",
        "v01.0.0",
        "v1.0.0+build.7",
        "v1.0.0;echo-owned",
        "v1.0.0 latest",
    ],
)
def test_release_tag_validation_rejects_invalid_or_injectable_tags(tag: str) -> None:
    result = subprocess.run(
        [str(RELEASE_SCRIPTS / "validate-release-tag.sh"), tag],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0


def test_deploy_script_executes_digest_pinned_helm_and_rollout_commands(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "calls.log"
    values = tmp_path / "values.yaml"
    values.write_text("config: {}\n")

    for binary in ("helm", "kubectl"):
        path = bin_dir / binary
        path.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$0 $*" >> "${CALL_LOG}"\n')
        path.chmod(0o755)

    digest = f"sha256:{'a' * 64}"
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["CALL_LOG"] = str(call_log)
    result = subprocess.run(
        [
            str(RELEASE_SCRIPTS / "deploy-oci-release.sh"),
            "--chart-ref",
            "oci://ghcr.io/example/charts/interlock",
            "--chart-digest",
            digest,
            "--image-repository",
            "ghcr.io/example/interlock-runtime",
            "--image-digest",
            digest,
            "--values",
            str(values),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    calls = call_log.read_text()
    assert f"oci://ghcr.io/example/charts/interlock@{digest}" in calls
    assert f"image.digest={digest}" in calls
    assert "--atomic --wait" in calls
    assert calls.count("rollout status") == 3


@pytest.mark.parametrize(
    ("live_claim_label", "transitions"),
    [("interlock-1.0.0-rc.3", True), ("", False)],
    ids=["versioned-claim-template", "stable-or-absent"],
)
def test_deploy_script_replaces_a_gateway_statefulset_with_a_versioned_claim_template(
    tmp_path: Path, live_claim_label: str, transitions: bool
) -> None:
    """Carry existing deployments across the claim-template label fix.

    Releases before the fix put per-release labels on the gateway's
    volumeClaimTemplates, an immutable field, so upgrading such a deployment to
    any newer chart failed and rolled back. The script replaces only the
    StatefulSet object - `--cascade=orphan` keeps its pod and volume, which the
    recreated StatefulSet adopts - and only while the live claim template still
    carries such a label.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "calls.log"
    values = tmp_path / "values.yaml"
    values.write_text("config: {}\n")
    (bin_dir / "helm").write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$0 $*" >> "${CALL_LOG}"\n')
    (bin_dir / "kubectl").write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$0 $*" >> "${CALL_LOG}"\n'
        'case "$*" in *"get statefulset"*) printf "%s" "${LIVE_CLAIM_LABEL}" ;; esac\n'
    )
    for binary in ("helm", "kubectl"):
        (bin_dir / binary).chmod(0o755)

    digest = f"sha256:{'b' * 64}"
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["CALL_LOG"] = str(call_log)
    env["LIVE_CLAIM_LABEL"] = live_claim_label
    result = subprocess.run(
        [
            str(RELEASE_SCRIPTS / "deploy-oci-release.sh"),
            "--chart-ref",
            "oci://ghcr.io/example/charts/interlock",
            "--chart-digest",
            digest,
            "--image-repository",
            "ghcr.io/example/interlock-runtime",
            "--image-digest",
            digest,
            "--values",
            str(values),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    calls = call_log.read_text().splitlines()
    upgrade_at = next(i for i, call in enumerate(calls) if "upgrade --install" in call)
    deletes = [
        i
        for i, call in enumerate(calls)
        if "delete statefulset interlock-gateway --cascade=orphan" in call
    ]
    if transitions:
        assert deletes and deletes[0] < upgrade_at
    else:
        assert deletes == []
    assert not any("delete" in call and "--cascade=orphan" not in call for call in calls)


def test_deploy_script_rejects_mutable_chart_or_image_coordinates(tmp_path: Path) -> None:
    values = tmp_path / "values.yaml"
    values.write_text("config: {}\n")
    result = subprocess.run(
        [
            str(RELEASE_SCRIPTS / "deploy-oci-release.sh"),
            "--chart-ref",
            "oci://ghcr.io/example/charts/interlock:latest",
            "--chart-digest",
            "latest",
            "--image-repository",
            "ghcr.io/example/interlock-runtime:latest",
            "--image-digest",
            "latest",
            "--values",
            str(values),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm CLI is not installed")
def test_chart_packages_with_release_version_override(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "helm",
            "package",
            str(ROOT / "deploy" / "helm" / "interlock"),
            "--version",
            "1.0.0-rc.1",
            "--app-version",
            "1.0.0-rc.1",
            "--destination",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "interlock-1.0.0-rc.1.tgz").exists()


def test_fixtures_script_never_rotates_existing_secrets() -> None:
    """Re-running the fixtures must not regenerate credentials.

    PostgreSQL fixes its superuser password during initdb and ignores
    POSTGRES_PASSWORD on subsequent starts. A second run that overwrites the
    credentials Secret therefore leaves the database on the old password, and
    every client - including the migration Job - fails with "password
    authentication failed". The same reasoning applies to the CA: replacing it
    while the server keeps its original certificate breaks verify-full.

    Found by running the script twice against a real cluster; the first
    "idempotence check" looked fine only because nothing had consumed the
    credentials yet.
    """
    body = (
        ROOT / "deploy" / "scripts" / "certification" / "bootstrap-cluster-fixtures.sh"
    ).read_text()

    # The guard must come before any create, inside the helper.
    helper = body.split("apply_secret()", 1)[1].split("\n}", 1)[0]
    assert "get secret" in helper, "apply_secret must check for an existing Secret"
    assert "return 0" in helper, "apply_secret must skip creation when one exists"
    assert helper.index("get secret") < helper.index(
        "create secret"
    ), "the existence check must precede creation"


def test_certification_negotiates_postgresql_client_tls() -> None:
    """The chart requires client TLS on the PostgreSQL listener.

    `pg_require_client_tls` defaults to False, and the compose stack leaves it
    there, so the suite historically only exercised the plaintext path. The
    chart hardcodes it true as a production invariant. Certification runs
    against a chart-deployed gateway, so it must negotiate TLS or every
    governed-query test fails with "PostgreSQL client TLS is required" -
    which is exactly what happened on the first real cluster run.
    """
    body = (ROOT / "deploy" / "scripts" / "certification" / "run-certification.sh").read_text()

    assert "INTERLOCK_PG_SSL" in body, "certification must request TLS on the PG listener"


def test_e2e_pg_tls_is_opt_in_so_compose_is_unaffected() -> None:
    """Turning TLS on for certification must not change the compose runs."""
    body = (ROOT / "tests" / "e2e" / "support" / "config.py").read_text()

    assert "_gateway_pg_ssl" in body
    # No env var set means no ssl argument, preserving the plaintext default.
    assert 'os.environ.get("INTERLOCK_PG_SSL", "")' in body
    assert "if not mode:" in body and "return None" in body


def test_github_attestation_steps_are_skipped_rather_than_ignored() -> None:
    """Attestation is conditional on visibility, never `continue-on-error`.

    GitHub's attestation store is a paid feature on private repositories, so
    the release cannot use it today. The difference that matters is between
    *skipping* a step that cannot work and *swallowing* its failure: the
    second would keep passing once the repository is public and the step
    should have worked, so a genuine regression in provenance would look
    exactly like today.

    The condition is on repository visibility, so attestation resumes on its
    own when the repository is published - with no workflow edit to remember.
    """
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    attest_steps = [step for step in steps if "actions/attest@" in step.get("uses", "")]

    assert len(attest_steps) == 4, [step.get("name") for step in attest_steps]
    for step in attest_steps:
        assert step.get("if") == "github.event.repository.visibility == 'public'", step.get("name")
        assert not step.get("continue-on-error"), (
            f"{step.get('name')} swallows failure instead of skipping; once the "
            "repository is public a real attestation failure would go unnoticed"
        )


def test_the_release_manifest_records_which_provenance_mechanisms_ran() -> None:
    """A consumer must be able to tell which verifications will work.

    Three mechanisms always run - the cosign signature, and the SLSA
    provenance and SPDX SBOM the build attaches in the registry. GitHub
    attestations do not, while the repository is private. Recording that in
    the manifest is what keeps "this release is attested" from being read as
    more than it is.
    """
    script = (ROOT / "deploy" / "scripts" / "release" / "create-release-manifest.sh").read_text()

    for field in (
        "cosign_signature",
        "registry_slsa_provenance",
        "registry_sbom_attestation",
        "github_attestations",
    ):
        assert field in script, f"{field} missing from the release manifest"
    assert "GITHUB_ATTESTATIONS" in script

    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "GITHUB_ATTESTATIONS: ${{ github.event.repository.visibility == 'public' }}" in workflow


def test_cloud_workflow_maps_every_state_variable_its_steps_read() -> None:
    """`terraform init` received an empty bucket: the steps read `TF_STATE_BUCKET`
    but no job env mapped it from the environment's variables, so the workflow
    could never have initialised its backend."""
    workflow = _workflow(CLOUD_WORKFLOW)
    for name, job in workflow["jobs"].items():
        runs = "\n".join(str(step.get("run", "")) for step in job.get("steps", []))
        used = set(re.findall(r"\$\{?(TF_STATE_[A-Z_]+)", runs))
        mapped = set((job.get("env") or {}).keys())
        assert used <= mapped, f"{name} reads {sorted(used - mapped)} without mapping them"
        for variable in used:
            assert job["env"][variable] == f"${{{{ vars.{variable} }}}}"
