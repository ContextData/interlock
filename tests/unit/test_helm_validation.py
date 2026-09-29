"""Validation tests for Helm chart YAML files."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

HELM_DIR = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "interlock"
TEMPLATES_DIR = HELM_DIR / "templates"


class TestChartYaml:
    def test_chart_yaml_exists(self) -> None:
        assert (HELM_DIR / "Chart.yaml").exists()

    def test_chart_yaml_parses(self) -> None:
        data = yaml.safe_load((HELM_DIR / "Chart.yaml").read_text())
        assert isinstance(data, dict)

    def test_chart_yaml_required_fields(self) -> None:
        data = yaml.safe_load((HELM_DIR / "Chart.yaml").read_text())
        assert data["apiVersion"] == "v2"
        assert data["name"] == "interlock"
        assert "version" in data
        assert "appVersion" in data
        assert "description" in data
        assert "InterLock" in data["description"]

    def test_chart_version_tracks_the_packaged_version(self) -> None:
        """A chart installed from source must default to the image it was built with.

        The image tag defaults to appVersion, so a stale appVersion here meant a
        source install pulled the first release candidate's image.
        """
        from interlock import release_version

        data = yaml.safe_load((HELM_DIR / "Chart.yaml").read_text())
        assert data["version"] == release_version()
        assert data["appVersion"] == release_version()

    def test_chart_names_its_home_and_maintainers(self) -> None:
        data = yaml.safe_load((HELM_DIR / "Chart.yaml").read_text())
        assert data["home"] == "https://github.com/ContextData/interlock"
        assert data["sources"] == ["https://github.com/ContextData/interlock"]
        assert data["maintainers"]


class TestValuesYaml:
    @pytest.fixture()
    def values(self) -> dict:
        return yaml.safe_load((HELM_DIR / "values.yaml").read_text())

    def test_values_yaml_exists(self) -> None:
        assert (HELM_DIR / "values.yaml").exists()

    def test_values_yaml_parses(self, values: dict) -> None:
        assert isinstance(values, dict)

    def test_image_section(self, values: dict) -> None:
        assert "image" in values
        assert values["image"]["repository"].endswith("/interlock-runtime")
        assert values["image"]["tag"] != "latest"
        assert "digest" in values["image"]

    def test_security_context_defaults(self, values: dict) -> None:
        pod_security = values["podSecurityContext"]
        container_security = values["containerSecurityContext"]

        assert pod_security["runAsNonRoot"] is True
        assert pod_security["runAsUser"] == 1000
        assert pod_security["runAsGroup"] == 1000
        assert pod_security["fsGroup"] == 1000
        assert pod_security["seccompProfile"]["type"] == "RuntimeDefault"
        assert container_security["allowPrivilegeEscalation"] is False
        assert container_security["readOnlyRootFilesystem"] is True
        assert container_security["capabilities"]["drop"] == ["ALL"]

    def test_gateway_section(self, values: dict) -> None:
        gw = values["gateway"]
        assert "replicas" in gw
        assert "upstreamPostgres" in gw
        assert gw["upstreamPostgres"]["host"]
        assert gw["upstreamPostgres"]["port"] == 5432
        assert "resources" in gw
        assert "service" in gw
        assert gw["service"]["httpPort"] == 3000
        assert gw["service"]["pgPort"] == 5432
        assert gw["probes"]["livenessPath"] == "/health"
        assert gw["probes"]["readinessPath"] == "/ready"
        assert gw["auditSpool"]["enabled"] is True
        assert gw["auditSpool"]["mountPath"] == "/var/lib/interlock/audit-spool"
        assert gw["auditSpool"]["accessModes"] == ["ReadWriteOnce"]
        assert gw["auditSpool"]["size"]

    def test_admin_section(self, values: dict) -> None:
        admin = values["admin"]
        assert "replicas" in admin
        assert "secret" in admin
        assert admin["secret"]["existingSecret"] == "interlock-admin-secret"
        assert admin["secret"]["secretKey"]
        assert admin["secret"]["value"] == ""
        assert admin["probes"]["readinessPath"] == "/ready"
        assert "resources" in admin
        assert "service" in admin

    def test_worker_section(self, values: dict) -> None:
        w = values["worker"]
        assert "replicas" in w
        assert "minReplicas" in w
        assert "maxReplicas" in w
        assert w["minReplicas"] <= w["maxReplicas"]
        assert "hpa" in w

    def test_migration_section(self, values: dict) -> None:
        migration = values["migration"]
        assert migration["enabled"] is True
        assert "backoffLimit" in migration
        assert "ttlSecondsAfterFinished" in migration
        assert "resources" in migration

    def test_database_section(self, values: dict) -> None:
        db = values["database"]
        assert "host" in db
        assert "port" in db
        assert "name" in db
        assert db["existingSecret"] == "interlock-db-credentials"
        assert db["sslMode"] == "verify-full"
        assert db["caExistingSecret"]

    def test_public_beta_security_secrets_are_explicit(self, values: dict) -> None:
        assert values["auth"]["apiKeyPepperExistingSecret"]
        assert values["auth"]["allowLegacySha256Keys"] is False
        assert values["gateway"]["pgTls"]["existingSecret"]
        assert values["config"]["auditDurabilityMode"] == "strict"

    def test_oidc_values_are_explicit_and_disabled_by_default(self, values: dict) -> None:
        oidc = values["auth"]["oidc"]
        assert oidc["enabled"] is False
        assert oidc["adminClientSecretKey"]
        assert oidc["localBreakGlassEnabled"] is True
        assert oidc["allowInsecureEndpoints"] is False
        assert oidc["adminGroupRoleMap"] == {}

        configmap = (TEMPLATES_DIR / "configmap.yaml").read_text()
        admin = (TEMPLATES_DIR / "admin-deployment.yaml").read_text()
        secrets = (TEMPLATES_DIR / "secret.yaml").read_text()
        assert "INTERLOCK_AUTH__OIDC__ISSUER_URL" in configmap
        assert "INTERLOCK_AUTH__OIDC__AGENT_AUDIENCE" in configmap
        assert "INTERLOCK_AUTH__OIDC__ADMIN_GROUP_ROLE_MAP" in configmap
        assert "INTERLOCK_AUTH__OIDC__ALLOW_INSECURE_ENDPOINTS" in configmap
        assert "INTERLOCK_AUTH__OIDC__ADMIN_CLIENT_SECRET" in admin
        assert "adminClientSecretExistingSecret is required" in secrets

    def test_admin_binds_all_interfaces_so_the_probe_can_reach_it(self) -> None:
        """The admin container must not bind loopback inside a pod.

        AdminConfig.host defaults to 127.0.0.1. The kubelet probes the
        container from outside the network namespace, so a loopback-bound
        admin never reports Ready, and deploy-oci-release.sh blocks on
        `rollout status deployment/<release>-admin` until it times out - which
        would abort a cloud certification run before it proved anything.
        """
        configmap = (TEMPLATES_DIR / "configmap.yaml").read_text()
        rendered = yaml.safe_load(
            "\n".join(
                line
                for line in configmap.splitlines()
                if "{{" not in line and not line.lstrip().startswith("#")
            )
        )

        assert rendered["data"]["INTERLOCK_ADMIN__HOST"] == "0.0.0.0"

    def test_redis_section(self, values: dict) -> None:
        assert "redis" in values
        assert "url" in values["redis"]

    def test_cache_config(self, values: dict) -> None:
        cache = values["config"]["cache"]
        assert "l1MaxSize" in cache
        assert "l1TtlSeconds" in cache
        assert "l2TtlSeconds" in cache

    def test_configmap_sets_gateway_upstream_postgres(self) -> None:
        content = (TEMPLATES_DIR / "configmap.yaml").read_text()

        assert "INTERLOCK_GATEWAY__UPSTREAM_PG_HOST" in content
        assert "INTERLOCK_GATEWAY__UPSTREAM_PG_PORT" in content

    def test_configmap_enables_production_security_contract(self) -> None:
        content = (TEMPLATES_DIR / "configmap.yaml").read_text()

        for setting in (
            "INTERLOCK_ENVIRONMENT",
            "INTERLOCK_DATABASE__SSL_MODE",
            "INTERLOCK_DATABASE__SSL_CA_FILE",
            "INTERLOCK_AUTH__ALLOW_LEGACY_SHA256_KEYS",
            "INTERLOCK_ADMIN__COOKIE_SECURE",
            "INTERLOCK_AUDIT__DURABILITY_MODE",
            "INTERLOCK_AUDIT__SPOOL_PATH",
            "INTERLOCK_GATEWAY__PG_REQUIRE_CLIENT_TLS",
        ):
            assert setting in content

    def test_gateway_uses_per_replica_persistent_audit_spool(self) -> None:
        content = (TEMPLATES_DIR / "gateway-deployment.yaml").read_text()

        assert "kind: StatefulSet" in content
        assert "volumeClaimTemplates:" in content
        assert "name: audit-spool" in content
        assert ".Values.gateway.auditSpool.mountPath" in content
        assert ".Values.gateway.auditSpool.existingClaim" in content

    def test_workloads_select_service_role_and_mount_control_db_ca(self) -> None:
        for template_name, role in (
            ("gateway-deployment.yaml", "gateway"),
            ("admin-deployment.yaml", "admin"),
            ("worker-deployment.yaml", "worker"),
            ("migration-job.yaml", "migration"),
        ):
            content = (TEMPLATES_DIR / template_name).read_text()
            assert "INTERLOCK_SERVICE_ROLE" in content
            assert f"value: {role}" in content
            assert "control-db-ca" in content

    def test_admin_deployment_uses_session_secret(self) -> None:
        content = (TEMPLATES_DIR / "admin-deployment.yaml").read_text()

        assert "INTERLOCK_ADMIN__SECRET_KEY" in content
        assert ".Values.admin.secret.existingSecret" in content
        assert ".Values.admin.secret.secretKey" in content

    def test_workload_templates_use_digest_capable_image_helper(self) -> None:
        helpers = (TEMPLATES_DIR / "_helpers.tpl").read_text()
        assert 'define "interlock.image"' in helpers
        assert ".Values.image.digest" in helpers

        for template_name in (
            "gateway-deployment.yaml",
            "admin-deployment.yaml",
            "worker-deployment.yaml",
            "migration-job.yaml",
        ):
            content = (TEMPLATES_DIR / template_name).read_text()
            assert '{{ include "interlock.image" . | quote }}' in content
            assert ".Values.image.repository }}:{{ .Values.image.tag" not in content

    def test_admin_and_gateway_readiness_use_ready_placeholder(self) -> None:
        for template_name, key in (
            ("gateway-deployment.yaml", ".Values.gateway.probes.readinessPath"),
            ("admin-deployment.yaml", ".Values.admin.probes.readinessPath"),
        ):
            content = (TEMPLATES_DIR / template_name).read_text()
            assert "readinessProbe:" in content
            assert key in content
            assert "path: /health" not in content

    def test_secret_template_never_defaults_to_empty_production_secrets(self) -> None:
        content = (TEMPLATES_DIR / "secret.yaml").read_text()

        assert "fail " in content
        assert "database.existingSecret or database.password is required" in content
        assert "admin.secret.existingSecret or admin.secret.value is required" in content
        assert 'default ""' not in content

    @pytest.mark.parametrize(
        "template_name",
        [
            "gateway-deployment.yaml",
            "admin-deployment.yaml",
            "worker-deployment.yaml",
            "migration-job.yaml",
        ],
    )
    def test_workload_templates_apply_security_contexts(self, template_name: str) -> None:
        content = (TEMPLATES_DIR / template_name).read_text()

        assert "toYaml .Values.podSecurityContext" in content
        assert "toYaml .Values.containerSecurityContext" in content

    @pytest.mark.parametrize(
        "template_name",
        [
            "gateway-deployment.yaml",
            "admin-deployment.yaml",
            "worker-deployment.yaml",
            "migration-job.yaml",
        ],
    )
    def test_workload_templates_mount_writable_paths(self, template_name: str) -> None:
        content = (TEMPLATES_DIR / template_name).read_text()

        assert "volumeMounts:" in content
        assert "mountPath: /tmp" in content
        assert "mountPath: /home/interlock" in content
        assert "emptyDir: {}" in content


class TestTemplates:
    """Verify all template YAML files are syntactically valid."""

    @pytest.fixture()
    def template_files(self) -> list[Path]:
        return sorted(TEMPLATES_DIR.glob("*.yaml"))

    def test_templates_directory_exists(self) -> None:
        assert TEMPLATES_DIR.exists()
        assert TEMPLATES_DIR.is_dir()

    def test_has_expected_templates(self, template_files: list[Path]) -> None:
        names = {f.name for f in template_files}
        expected = {
            "gateway-deployment.yaml",
            "gateway-service.yaml",
            "admin-deployment.yaml",
            "admin-service.yaml",
            "worker-deployment.yaml",
            "worker-hpa.yaml",
            "migration-job.yaml",
            "configmap.yaml",
            "secret.yaml",
            "ingress.yaml",
            "gateway-pg-service.yaml",
            "admin-ingress.yaml",
        }
        assert expected.issubset(names), f"Missing templates: {expected - names}"

    def test_helpers_tpl_exists(self) -> None:
        assert (TEMPLATES_DIR / "_helpers.tpl").exists()

    @pytest.mark.parametrize(
        "template_name",
        [
            "configmap.yaml",
            "secret.yaml",
            "gateway-deployment.yaml",
            "gateway-service.yaml",
            "admin-deployment.yaml",
            "admin-service.yaml",
            "worker-deployment.yaml",
            "worker-hpa.yaml",
            "migration-job.yaml",
            "ingress.yaml",
            "gateway-pg-service.yaml",
            "admin-ingress.yaml",
        ],
    )
    def test_template_contains_valid_yaml_structure(self, template_name: str) -> None:
        """Verify templates contain recognizable Kubernetes resource markers.

        We cannot fully parse Helm templates with yaml.safe_load because
        they contain Go template directives ({{ }}). Instead we check that
        key structural elements are present.
        """
        content = (TEMPLATES_DIR / template_name).read_text()
        # Every template should reference apiVersion (possibly templated)
        assert "apiVersion:" in content or "apiVersion" in content
        # Every template should have a kind
        assert "kind:" in content
        # Every template should have metadata
        assert "metadata:" in content


class TestOperatorValuesFiles:
    """Guards for the two shipped values files.

    `values.cloud.example.yaml` is what an operator copies; if it drifts from
    what the chart actually requires, the first deployment fails on a missing
    value. `values.certification.yaml` drives the cloud-certification run and
    must keep the production invariants, since certifying a relaxed
    configuration certifies nothing.
    """

    CLOUD_EXAMPLE = HELM_DIR.parent / "values.cloud.example.yaml"
    CERTIFICATION = HELM_DIR.parent / "values.certification.yaml"

    @staticmethod
    def _chart_required_value_paths() -> set[str]:
        """Value paths the chart marks `required` and cannot render without."""
        paths: set[str] = set()
        for template in TEMPLATES_DIR.glob("*.yaml"):
            for match in re.finditer(
                r'required\s+"[^"]+"\s+\.Values\.([A-Za-z0-9_.]+)', template.read_text()
            ):
                paths.add(match.group(1))
        return paths

    @staticmethod
    def _get(values: dict, dotted: str):
        node = values
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def test_both_files_parse(self) -> None:
        for path in (self.CLOUD_EXAMPLE, self.CERTIFICATION):
            assert path.exists(), path
            assert isinstance(yaml.safe_load(path.read_text()), dict), path

    def test_certification_values_satisfy_every_chart_required_secret(self) -> None:
        """The unconditional `required` references must all be set.

        These three have no chart-side fallback, so an unset value aborts
        `helm install` before anything deploys.
        """
        values = yaml.safe_load(self.CERTIFICATION.read_text())
        for dotted in (
            "auth.apiKeyPepperExistingSecret",
            "database.caExistingSecret",
            "gateway.pgTls.existingSecret",
        ):
            assert (
                dotted in self._chart_required_value_paths()
            ), f"{dotted} is no longer chart-required; update this guard"
            assert self._get(values, dotted), f"{dotted} unset in certification values"

    def test_cloud_example_documents_every_chart_required_secret(self) -> None:
        body = self.CLOUD_EXAMPLE.read_text()
        for dotted in sorted(self._chart_required_value_paths()):
            leaf = dotted.split(".")[-1]
            assert leaf in body, f"{dotted} is chart-required but undocumented"

    def test_certification_values_keep_production_invariants(self) -> None:
        values = yaml.safe_load(self.CERTIFICATION.read_text())

        assert values["database"]["sslMode"] == "verify-full"
        assert values["auth"]["allowLegacySha256Keys"] is False
        assert values["config"]["auditDurabilityMode"] == "strict"
        assert values["gateway"]["pgTls"]["trustedOffload"] is False
        assert values["gateway"]["auditSpool"]["enabled"] is True

    def test_values_files_carry_no_inline_credentials(self) -> None:
        """Secrets are referenced by Secret name, never inlined."""
        for path in (self.CLOUD_EXAMPLE, self.CERTIFICATION):
            values = yaml.safe_load(path.read_text())
            assert not self._get(values, "admin.secret.value"), path
            assert not self._get(values, "database.password"), path
            assert not self._get(values, "database.user"), path

    def test_cloud_example_lists_the_secrets_an_operator_must_precreate(self) -> None:
        body = self.CLOUD_EXAMPLE.read_text()
        for secret_key in ("secret-key", "pepper", "ca.crt", "tls.crt", "tls.key"):
            assert secret_key in body, f"{secret_key} not documented"


def test_fixtures_script_creates_every_secret_the_chart_requires() -> None:
    """The certification fixtures and the chart must agree on Secret names.

    The chart marks three of these `required`, so a name drifting on either
    side fails `helm install` in the cloud - after a cluster has been paid
    for. Compare the rendered requirement against what the script creates
    rather than trusting two hand-maintained lists.
    """
    import re

    templates = "\n".join(path.read_text() for path in sorted(TEMPLATES_DIR.glob("*.yaml")))
    # Secret names the workloads reference, as chart values.
    referenced_values = set(re.findall(r"\.Values\.([A-Za-z0-9_.]*[Ee]xistingSecret)", templates))
    values = yaml.safe_load((HELM_DIR.parent / "values.certification.yaml").read_text())

    def resolve(dotted: str):
        node = values
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    required_names = {name for name in (resolve(path) for path in referenced_values) if name}
    assert required_names, "no existingSecret references resolved"

    script = (
        HELM_DIR.parents[1] / "scripts" / "certification" / "bootstrap-cluster-fixtures.sh"
    ).read_text()
    created = set(re.findall(r"apply_secret (?:generic|tls) ([a-z0-9-]+)", script))

    missing = required_names - created
    assert not missing, f"fixtures script does not create: {sorted(missing)}"


def test_migration_hook_dependencies_are_created_before_the_job() -> None:
    """Helm applies hooks before the rest of the release.

    The migration Job is a pre-install/pre-upgrade hook that reads the config
    ConfigMap and the database Secret. If those are ordinary release
    resources, Helm creates them only after hooks have run, so the Job fails
    with `configmap "<release>-config" not found` and the whole install rolls
    back. A real `helm install` is the only thing that catches this - the
    chart renders perfectly either way.

    They must therefore be hooks too, with a lower hook-weight than the Job.
    """
    job = (TEMPLATES_DIR / "migration-job.yaml").read_text()
    assert '"helm.sh/hook": pre-install,pre-upgrade' in job

    for name in ("configmap.yaml", "secret.yaml"):
        body = (TEMPLATES_DIR / name).read_text()
        assert '"helm.sh/hook": pre-install,pre-upgrade' in body, name
        # The Job runs at the default weight of 0, so its dependencies must
        # carry a negative weight to be applied first.
        weights = re.findall(r'"helm\.sh/hook-weight":\s*"(-?\d+)"', body)
        assert weights, f"{name} has no hook-weight"
        assert all(int(w) < 0 for w in weights), f"{name} weights must be negative: {weights}"


def test_audit_spool_path_is_a_subdirectory_of_the_mounted_volume() -> None:
    """The spool must never be the volume root.

    `_verify_spool_directory` requires mode 0700 and chmods the directory when
    it is not. A cloud block-storage volume mounts root-owned - fsGroup sets
    the volume's group, not its owner - and chmod requires ownership, so
    chmod-ing the mount point fails with EPERM. Under strict audit durability
    that aborts gateway startup, which means the gateway cannot start on any
    cloud PVC.

    Pointing the spool at a subdirectory lets the container create it as its
    own uid and therefore chmod it. Verified against DigitalOcean block
    storage: mounting at the spool path reproduced
    `PermissionError: [Errno 1] Operation not permitted`.
    """
    configmap = (TEMPLATES_DIR / "configmap.yaml").read_text()
    gateway = (TEMPLATES_DIR / "gateway-deployment.yaml").read_text()

    spool_line = next(
        line for line in configmap.splitlines() if "INTERLOCK_AUDIT__SPOOL_PATH" in line
    )
    assert "printf" in spool_line and "/spool" in spool_line, (
        "the spool path must be a subdirectory of auditSpool.mountPath, not the "
        f"mount point itself: {spool_line.strip()}"
    )
    # The volume itself is still mounted at the configured mountPath.
    assert "mountPath: {{ .Values.gateway.auditSpool.mountPath | quote }}" in gateway


class TestAdditiveOperatorValues:
    """Values a real deployment needs that the chart did not expose.

    Every one of these was found by trying to deploy InterLock to a managed
    Kubernetes cluster against real data sources. Without them the chart can
    render a healthy-looking release that cannot actually work:

      - connector credentials are `env://` references resolved inside the pod
        (`src/interlock/secrets/resolver.py`), and nothing could put those
        variables in the pod, so every source failed closed;
      - the first admin is created from `INTERLOCK_ADMIN__BOOTSTRAP_PASSWORD`
        (`src/interlock/admin/app.py`), which the chart never set, so there
        was no way to log in;
      - a private registry needs an image pull secret, including on the
        migration Job, which is a pre-install hook and pulls the same image;
      - `INTERLOCK_REDIS__URL` sat in a plaintext ConfigMap, and a managed
        Redis requires a password inside that URL;
      - the gateway Service carries both the HTTP and the PostgreSQL wire
        port, so exposing PG-wire through a load balancer also exposed HTTP;
      - the Ingress routes only the gateway, and admin cookies are
        unconditionally `Secure`, so the console was unreachable.

    All are additive and default to off, which `docs-site/src/content/docs/reference/contracts/helm-v1.md`
    treats as a compatible change. These tests assert the wiring rather than
    the rendering; `TestRenderedAdditiveValues` renders it for real.
    """

    WORKLOADS = (
        "gateway-deployment.yaml",
        "admin-deployment.yaml",
        "worker-deployment.yaml",
        "migration-job.yaml",
    )

    @pytest.fixture()
    def values(self) -> dict:
        return yaml.safe_load((HELM_DIR / "values.yaml").read_text())

    def test_image_pull_secrets_reach_every_pod_including_the_migration_hook(
        self, values: dict
    ) -> None:
        """The hook pulls the same image, so omitting it fails before install."""
        assert values["imagePullSecrets"] == []
        assert 'define "interlock.imagePullSecrets"' in (TEMPLATES_DIR / "_helpers.tpl").read_text()

        for name in self.WORKLOADS:
            assert (
                "interlock.imagePullSecrets" in (TEMPLATES_DIR / name).read_text()
            ), f"{name} cannot pull from a private registry"

    ROLLING_WORKLOADS = (
        "gateway-deployment.yaml",
        "admin-deployment.yaml",
        "worker-deployment.yaml",
    )

    def test_long_running_workloads_roll_when_their_configuration_changes(self) -> None:
        """A ConfigMap-only upgrade must reach running pods.

        Configuration arrives through `envFrom`, which a container reads once at
        start. Without a checksum of the rendered ConfigMap on the pod template,
        `helm upgrade` rewrote the ConfigMap, reported success and rolled
        nothing - verified live, where a changed value stayed invisible to the
        running gateway until a manual restart. The migration Job is excluded:
        it is a hook recreated on every upgrade already.
        """
        helpers = (TEMPLATES_DIR / "_helpers.tpl").read_text()
        assert 'define "interlock.podAnnotations"' in helpers
        assert 'include (print $.Template.BasePath "/configmap.yaml") . | sha256sum' in helpers
        for name in self.ROLLING_WORKLOADS:
            assert (
                "interlock.podAnnotations" in (TEMPLATES_DIR / name).read_text()
            ), f"{name} would not roll when its configuration changes"

    @pytest.mark.parametrize(
        ("component", "template_name"),
        [
            ("gateway", "gateway-deployment.yaml"),
            ("admin", "admin-deployment.yaml"),
            ("worker", "worker-deployment.yaml"),
        ],
    )
    def test_components_accept_extra_env_and_env_from(
        self, values: dict, component: str, template_name: str
    ) -> None:
        """Admin needs these too: it resolves the same refs when probing a source."""
        assert values[component]["extraEnv"] == []
        assert values[component]["extraEnvFrom"] == []

        content = (TEMPLATES_DIR / template_name).read_text()
        assert f".Values.{component}.extraEnv" in content
        assert f".Values.{component}.extraEnvFrom" in content

    def test_admin_bootstrap_password_is_opt_in_and_secret_backed(self, values: dict) -> None:
        assert values["admin"]["bootstrapPasswordExistingSecret"] == ""
        assert values["admin"]["bootstrapPasswordSecretKey"] == "password"

        content = (TEMPLATES_DIR / "admin-deployment.yaml").read_text()
        assert "INTERLOCK_ADMIN__BOOTSTRAP_PASSWORD" in content
        assert "if .Values.admin.bootstrapPasswordExistingSecret" in content
        assert "secretKeyRef" in content
        # A literal password in the manifest would be readable by anyone with
        # `helm get values`; only a Secret reference is acceptable.
        assert not re.search(
            r"INTERLOCK_ADMIN__BOOTSTRAP_PASSWORD\s*\n\s*value:", content
        ), "bootstrap password must never be an inline value"

    def test_redis_url_can_be_secret_backed_and_leaves_the_configmap_when_it_is(
        self, values: dict
    ) -> None:
        assert values["redis"]["existingSecret"] == ""
        assert values["redis"]["secretUrlKey"] == "url"

        configmap = (TEMPLATES_DIR / "configmap.yaml").read_text()
        assert "if not .Values.redis.existingSecret" in configmap

        assert 'define "interlock.redisEnv"' in (TEMPLATES_DIR / "_helpers.tpl").read_text()
        for name in self.WORKLOADS:
            assert (
                "interlock.redisEnv" in (TEMPLATES_DIR / name).read_text()
            ), f"{name} would lose its Redis URL when the Secret is used"

    def test_gateway_service_accepts_annotations(self, values: dict) -> None:
        assert values["gateway"]["service"]["annotations"] == {}
        assert (
            ".Values.gateway.service.annotations"
            in (TEMPLATES_DIR / "gateway-service.yaml").read_text()
        )

    def test_gateway_pg_service_is_optional_and_exposes_only_the_pg_port(
        self, values: dict
    ) -> None:
        """Exposing PG-wire must not drag the HTTP port onto the same LB."""
        pg_service = values["gateway"]["pgService"]
        assert pg_service["enabled"] is False
        assert pg_service["annotations"] == {}

        content = (TEMPLATES_DIR / "gateway-pg-service.yaml").read_text()
        assert "if .Values.gateway.pgService.enabled" in content
        assert "targetPort: pg" in content
        assert "targetPort: http" not in content

    def test_admin_ingress_is_optional_and_targets_the_admin_service(self, values: dict) -> None:
        ingress = values["admin"]["ingress"]
        assert ingress["enabled"] is False
        assert ingress["hosts"] == []
        assert ingress["tls"] == []

        content = (TEMPLATES_DIR / "admin-ingress.yaml").read_text()
        assert "if .Values.admin.ingress.enabled" in content
        assert "-admin" in content
        assert "kind: Ingress" in content

    def test_the_new_values_are_not_chart_required(self) -> None:
        """Additive means the default render must still work untouched.

        `required` would make an existing deployment fail on upgrade, which is
        the opposite of an additive value.
        """
        joined = "\n".join(path.read_text() for path in sorted(TEMPLATES_DIR.glob("*.yaml")))
        for value_path in (
            "imagePullSecrets",
            "admin.bootstrapPasswordExistingSecret",
            "redis.existingSecret",
            "gateway.pgService.enabled",
            "admin.ingress.enabled",
        ):
            assert not re.search(
                rf'required\s+"[^"]+"\s+\.Values\.{re.escape(value_path)}\b', joined
            ), f"{value_path} must not be chart-required"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI is not installed")
class TestRenderedAdditiveValues:
    """Render the chart with every new value set and inspect the manifests.

    Asserting on template text proves the wiring exists; only rendering proves
    it produces valid Kubernetes objects. A prior DOKS install failed on three
    bugs that every static check passed, so the chart earns a real render.
    """

    @staticmethod
    def _render(extra_args: list[str]) -> list[dict]:
        result = subprocess.run(
            ["helm", "template", "interlock", str(HELM_DIR), *extra_args],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr[-3000:]
        return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]

    @pytest.fixture(scope="class")
    def default_docs(self) -> list[dict]:
        return self._render([])

    @staticmethod
    def _images(docs: list[dict]) -> set[str]:
        return {
            container["image"]
            for doc in docs
            for container in (
                doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
            )
        }

    def test_default_image_is_the_charts_own_app_version(self, default_docs: list[dict]) -> None:
        app_version = yaml.safe_load((HELM_DIR / "Chart.yaml").read_text())["appVersion"]
        assert self._images(default_docs) == {
            f"ghcr.io/contextdata/interlock-runtime:{app_version}"
        }

    def test_an_explicit_tag_or_digest_overrides_the_app_version(self) -> None:
        tagged = self._render(["--set", "image.tag=9.9.9"])
        assert self._images(tagged) == {"ghcr.io/contextdata/interlock-runtime:9.9.9"}
        digest = "sha256:" + "a" * 64
        pinned = self._render(["--set", f"image.digest={digest}"])
        assert self._images(pinned) == {f"ghcr.io/contextdata/interlock-runtime@{digest}"}

    @pytest.fixture(scope="class")
    def configured_docs(self) -> list[dict]:
        return self._render(
            [
                "--set",
                "imagePullSecrets[0].name=ghcr-pull",
                "--set",
                "redis.existingSecret=interlock-redis",
                "--set",
                "admin.bootstrapPasswordExistingSecret=interlock-admin-bootstrap",
                "--set",
                "gateway.pgService.enabled=true",
                "--set",
                "gateway.service.annotations.example\\.com/purpose=http",
                "--set",
                "admin.ingress.enabled=true",
                "--set",
                "admin.ingress.className=nginx",
                "--set",
                "admin.ingress.hosts[0].host=admin.example.com",
                "--set",
                "admin.ingress.hosts[0].paths[0].path=/",
                "--set",
                "admin.ingress.hosts[0].paths[0].pathType=Prefix",
                "--set-json",
                'gateway.extraEnvFrom=[{"secretRef":{"name":"interlock-source-credentials"}}]',
                "--set-json",
                'admin.extraEnvFrom=[{"secretRef":{"name":"interlock-source-credentials"}}]',
                "--set-json",
                'admin.extraEnv=[{"name":"FORWARDED_ALLOW_IPS","value":"*"}]',
            ]
        )

    @staticmethod
    def _pod_specs(docs: list[dict]) -> dict[str, dict]:
        specs: dict[str, dict] = {}
        for doc in docs:
            if doc.get("kind") in {"Deployment", "StatefulSet", "Job"}:
                specs[doc["metadata"]["name"]] = doc["spec"]["template"]["spec"]
        return specs

    @staticmethod
    def _config_checksums(docs: list[dict]) -> dict[str, str | None]:
        sums: dict[str, str | None] = {}
        for doc in docs:
            if doc.get("kind") in {"Deployment", "StatefulSet"}:
                metadata = doc["spec"]["template"].get("metadata") or {}
                sums[doc["metadata"]["name"]] = (metadata.get("annotations") or {}).get(
                    "checksum/config"
                )
        return sums

    def test_every_long_running_workload_carries_the_same_config_checksum(
        self, default_docs: list[dict]
    ) -> None:
        sums = self._config_checksums(default_docs)
        assert sorted(name.rsplit("-", 1)[-1] for name in sums) == ["admin", "gateway", "worker"]
        assert all(sums.values()), f"a workload has no config checksum: {sums}"
        assert len(set(sums.values())) == 1, "the three workloads read one ConfigMap"

    def test_a_configmap_value_change_changes_the_checksum(self, default_docs: list[dict]) -> None:
        """The exact change that stayed inert on the live deployment."""
        before = self._config_checksums(default_docs)
        after = self._config_checksums(self._render(["--set", "approvals.expirySeconds=3601"]))
        for name, value in before.items():
            assert after[name] != value, f"{name} would not roll for a ConfigMap change"

    def test_a_change_outside_the_configmap_leaves_the_checksum_alone(
        self, default_docs: list[dict]
    ) -> None:
        """Content-derived, not a roll-on-every-upgrade token."""
        before = self._config_checksums(default_docs)
        after = self._config_checksums(self._render(["--set", "gateway.replicas=2"]))
        assert after == before

    def test_the_gateway_claim_template_labels_do_not_change_between_releases(
        self, default_docs: list[dict]
    ) -> None:
        """A StatefulSet's volumeClaimTemplates cannot be changed in place.

        The claim template used to carry the full label set, including
        `helm.sh/chart` and `app.kubernetes.io/version`, which the release
        workflow stamps per release. Every release-to-release `helm upgrade`
        therefore asked Kubernetes to change an immutable field, failed, and
        rolled back - found on the first rc.3 -> rc.4 in-place upgrade.
        """
        gateway = next(
            doc
            for doc in default_docs
            if doc.get("kind") == "StatefulSet" and doc["metadata"]["name"].endswith("-gateway")
        )
        templates = gateway["spec"].get("volumeClaimTemplates") or []
        assert templates, "the default render should include the audit spool claim template"
        for template in templates:
            labels = template["metadata"].get("labels") or {}
            assert "helm.sh/chart" not in labels
            assert "app.kubernetes.io/version" not in labels
            assert labels.get("app.kubernetes.io/name")
            assert labels.get("app.kubernetes.io/instance")
            assert labels.get("app.kubernetes.io/component") == "gateway"

    def test_the_default_render_is_unchanged_by_the_new_values(
        self, default_docs: list[dict]
    ) -> None:
        """An operator who upgrades without opting in must see no difference."""
        specs = self._pod_specs(default_docs)
        assert specs, "nothing rendered"
        for name, spec in specs.items():
            assert "imagePullSecrets" not in spec, name

        kinds = [doc["kind"] for doc in default_docs]
        assert kinds.count("Ingress") == 0
        pg_services = [
            doc
            for doc in default_docs
            if doc["kind"] == "Service" and doc["metadata"]["name"].endswith("-gateway-pg")
        ]
        assert pg_services == []

    def test_every_pod_including_the_migration_hook_gets_the_pull_secret(
        self, configured_docs: list[dict]
    ) -> None:
        specs = self._pod_specs(configured_docs)
        assert len(specs) == 4, sorted(specs)
        for name, spec in specs.items():
            assert spec.get("imagePullSecrets") == [{"name": "ghcr-pull"}], name

    def test_the_redis_url_leaves_the_configmap_and_arrives_from_the_secret(
        self, configured_docs: list[dict]
    ) -> None:
        configmaps = [doc for doc in configured_docs if doc["kind"] == "ConfigMap"]
        assert configmaps
        for configmap in configmaps:
            assert "INTERLOCK_REDIS__URL" not in configmap.get("data", {})

        for name, spec in self._pod_specs(configured_docs).items():
            env = {entry["name"]: entry for entry in spec["containers"][0].get("env", [])}
            assert "INTERLOCK_REDIS__URL" in env, name
            ref = env["INTERLOCK_REDIS__URL"]["valueFrom"]["secretKeyRef"]
            assert ref == {"name": "interlock-redis", "key": "url"}, name

    def test_extra_env_and_env_from_reach_the_containers(self, configured_docs: list[dict]) -> None:
        specs = self._pod_specs(configured_docs)
        for name in ("interlock-gateway", "interlock-admin"):
            container = specs[name]["containers"][0]
            assert {"secretRef": {"name": "interlock-source-credentials"}} in container[
                "envFrom"
            ], name

        admin_env = {
            entry["name"]: entry.get("value")
            for entry in specs["interlock-admin"]["containers"][0]["env"]
        }
        assert admin_env["FORWARDED_ALLOW_IPS"] == "*"
        assert "INTERLOCK_ADMIN__BOOTSTRAP_PASSWORD" in admin_env

    def test_the_pg_service_carries_only_the_wire_port(self, configured_docs: list[dict]) -> None:
        pg_service = next(
            doc
            for doc in configured_docs
            if doc["kind"] == "Service" and doc["metadata"]["name"].endswith("-gateway-pg")
        )
        ports = pg_service["spec"]["ports"]
        assert [port["targetPort"] for port in ports] == ["pg"]
        assert pg_service["spec"]["type"] == "LoadBalancer"
        assert pg_service["spec"]["selector"]["app.kubernetes.io/component"] == "gateway"

        http_service = next(
            doc
            for doc in configured_docs
            if doc["kind"] == "Service" and doc["metadata"]["name"].endswith("-gateway")
        )
        assert http_service["metadata"]["annotations"] == {"example.com/purpose": "http"}

    def test_the_admin_ingress_routes_to_the_admin_service(
        self, configured_docs: list[dict]
    ) -> None:
        ingresses = [doc for doc in configured_docs if doc["kind"] == "Ingress"]
        assert len(ingresses) == 1, [doc["metadata"]["name"] for doc in ingresses]
        ingress = ingresses[0]
        assert ingress["spec"]["ingressClassName"] == "nginx"
        rule = ingress["spec"]["rules"][0]
        assert rule["host"] == "admin.example.com"
        backend = rule["http"]["paths"][0]["backend"]["service"]
        assert backend["name"].endswith("-admin")
        assert backend["port"] == {"name": "http"}
