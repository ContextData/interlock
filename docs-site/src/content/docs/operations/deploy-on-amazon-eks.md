---
title: Deploy on Amazon EKS
description: Run InterLock on Amazon EKS with RDS PostgreSQL and ElastiCache, the signed Helm chart, and a load balancer, without a domain.
sidebar:
  order: 6
---

This runs InterLock on an Amazon EKS cluster with its state in RDS PostgreSQL
and ElastiCache (Valkey), created by the Terraform in the repository. The
gateway is reached through one Network Load Balancer:

| Port | Serves | TLS |
|---|---|---|
| `3000` | HTTP proxy and MCP | Terminated at the load balancer |
| `5432` | PostgreSQL wire protocol | Terminated by the gateway |

The admin console is not exposed: you reach it through `kubectl
port-forward`.

This guide uses the load balancer's own AWS name and a certificate from a
certificate authority you create, so it needs no domain. Clients must be given
that authority's certificate to trust the gateway, which is fine for agents
you run yourself but rules out hosted agents (the OpenAI and Gemini
interactions APIs, the Anthropic API's MCP connector), which accept only
publicly trusted certificates. For those, put the gateway behind a domain you
own with a public certificate.

It takes about an hour, most of it waiting for EKS and RDS. For a quick
evaluation on one machine, use [an EC2 instance](/operations/deploy-on-an-aws-ec2-instance/)
instead.

## What you need

- An AWS account and the [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html),
  signed in with permission to create VPCs, EKS, RDS, ElastiCache, IAM roles
  and ACM certificates.
- Terraform 1.11 or later, `kubectl` within one minor version of the cluster,
  `helm` 3.8 or later, `openssl`, `jq` and `psql`.
- A clone of this repository at the release you install; the Terraform is in
  it.

Set the names used throughout. Everything below reads these variables:

```bash
export AWS_REGION=us-east-1
export NAME=interlock
export VERSION=1.0.0-rc.16                   # the InterLock release to install
export ADMIN_CIDR="$(curl -4 -fsS https://checkip.amazonaws.com)/32"   # may reach the cluster API
export STATE_BUCKET="interlock-tfstate-$(aws sts get-caller-identity --query Account --output text)-$AWS_REGION"
export TF=deploy/terraform/environments/aws

git clone --depth 1 --branch "v$VERSION" https://github.com/ContextData/interlock
cd interlock
```

## 1. Create the state bucket

Terraform keeps its state in S3. The state includes the cache's AUTH value, so
the bucket is private, versioned and encrypted:

```bash
if [ "$AWS_REGION" = us-east-1 ]; then
  aws s3api create-bucket --bucket "$STATE_BUCKET" > /dev/null
else
  aws s3api create-bucket --bucket "$STATE_BUCKET" \
    --create-bucket-configuration LocationConstraint="$AWS_REGION" > /dev/null
fi
aws s3api put-bucket-versioning --bucket "$STATE_BUCKET" --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket "$STATE_BUCKET" \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

## 2. Create the cluster and databases

Terraform takes the release's exact artifacts from its manifest, then creates
a VPC with one NAT gateway, the EKS cluster with on-demand nodes, a private
RDS PostgreSQL instance and a TLS-only ElastiCache cache, both reachable only
from the cluster's nodes:

```bash
curl -fsSLo release-manifest.json \
  "https://github.com/ContextData/interlock/releases/download/v$VERSION/release-manifest.json"
IMAGE="$(jq -r .artifacts.image release-manifest.json)"
CHART="$(jq -r .artifacts.helm_chart release-manifest.json)"
export TF_VAR_region="$AWS_REGION" TF_VAR_cluster_name="$NAME" TF_VAR_kubernetes_version=1.36
export TF_VAR_control_plane_allowed_cidrs="[\"$ADMIN_CIDR\"]"
export TF_VAR_release_version="$VERSION"
export TF_VAR_image_repository="${IMAGE%@*}" TF_VAR_image_digest="${IMAGE#*@}"
export TF_VAR_helm_chart_ref="oci://${CHART%@*}" TF_VAR_helm_chart_digest="${CHART#*@}"
export TF_VAR_managed_data_tier=true TF_VAR_node_capacity_type=ON_DEMAND

terraform -chdir="$TF" init -backend-config="bucket=$STATE_BUCKET" \
  -backend-config="key=interlock/$NAME.tfstate" -backend-config="region=$AWS_REGION" \
  -backend-config="encrypt=true" -backend-config="use_lockfile=true"
terraform -chdir="$TF" apply
eval "$(terraform -chdir="$TF" output -raw kubeconfig_command)"
kubectl get nodes
```

The cluster API accepts connections only from `ADMIN_CIDR`. If your address
changes, update `TF_VAR_control_plane_allowed_cidrs` and apply again.

## 3. Prepare the control database

InterLock needs its own login and database, and two extensions that only the
master user can install. RDS is private, so this runs as a one-off job inside
the cluster. First give the cluster the RDS certificate authorities, which
InterLock also uses to verify the database (`sslmode: verify-full`):

```bash
export DB_HOST="$(terraform -chdir="$TF" output -raw control_db_host)"
curl -fsSo rds-ca.pem "https://truststore.pki.rds.amazonaws.com/$AWS_REGION/$AWS_REGION-bundle.pem"
kubectl create namespace interlock
kubectl -n interlock create secret generic interlock-control-db-ca --from-file=ca.crt=rds-ca.pem
```

InterLock's login gets a generated password, and the master password stays in
Secrets Manager; both reach the job through a temporary Secret and are never
printed:

```bash
DB_PASSWORD="$(openssl rand -hex 24)"
kubectl -n interlock create secret generic interlock-db-credentials \
  --from-literal=username=interlock --from-literal=password="$DB_PASSWORD"
kubectl -n interlock create secret generic interlock-db-setup \
  --from-literal=PGHOST="$DB_HOST" --from-literal=PGUSER=interlock_admin \
  --from-literal=PGPASSWORD="$(aws secretsmanager get-secret-value \
    --secret-id "$(terraform -chdir="$TF" output -raw control_db_master_secret_arn)" \
    --query SecretString --output text | jq -r .password)" \
  --from-literal=DB_PASSWORD="$DB_PASSWORD"
unset DB_PASSWORD

kubectl apply -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: interlock-db-setup
  namespace: interlock
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: psql
          image: postgres:17
          envFrom: [{secretRef: {name: interlock-db-setup}}]
          env:
            - {name: PGDATABASE, value: postgres}
            - {name: PGSSLMODE, value: verify-full}
            - {name: PGSSLROOTCERT, value: /ca/ca.crt}
          command: [bash, -ec]
          args:
            - |
              psql -v ON_ERROR_STOP=1 -v pw="$DB_PASSWORD" <<'SQL'
              CREATE ROLE interlock LOGIN PASSWORD :'pw';
              GRANT interlock TO CURRENT_USER;
              CREATE DATABASE interlock OWNER interlock;
              SQL
              psql -v ON_ERROR_STOP=1 -d interlock \
                -c 'CREATE EXTENSION IF NOT EXISTS pgcrypto' \
                -c 'CREATE EXTENSION IF NOT EXISTS ltree'
              psql -tA -c 'SHOW max_connections'
          volumeMounts: [{name: ca, mountPath: /ca}]
      volumes: [{name: ca, secret: {secretName: interlock-control-db-ca}}]
EOF
kubectl -n interlock wait job/interlock-db-setup --for=condition=Complete --timeout=5m
kubectl -n interlock logs job/interlock-db-setup | tail -1
kubectl -n interlock delete job/interlock-db-setup secret/interlock-db-setup
```

`GRANT interlock TO CURRENT_USER` is needed because, on PostgreSQL 16 and
later, the RDS master user may create a database owned by another role only
if it is a member of that role. The last line printed is the database's
connection limit: 181 on the default `db.t4g.small`. Every InterLock pod keeps a pool of connections open, up to
`INTERLOCK_DATABASE__MAX_POOL` plus one it listens on, and a rolling upgrade
briefly runs an extra pod: keep pods × (pool + 1), plus two pods for upgrades,
below that limit.

## 4. Create a certificate authority and the gateway certificate

The load balancer's name is not known until it exists, and no public
authority issues certificates for AWS's own names. So the gateway's
certificate is issued by an authority you create, for every load balancer name
in the region:

```bash
umask 077
openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=InterLock CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout ca.key -out ca.pem
openssl req -newkey rsa:2048 -nodes -subj "/CN=interlock-gateway" -keyout gateway.key -out gateway.csr
openssl x509 -req -in gateway.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 397 -out gateway.crt \
  -extfile <(printf 'subjectAltName=DNS:*.elb.%s.amazonaws.com\nextendedKeyUsage=serverAuth\n' "$AWS_REGION")
umask 022
```

The load balancer presents the certificate for HTTPS, from AWS Certificate
Manager; the gateway presents the same one for PostgreSQL:

```bash
export CERT_ARN="$(aws acm import-certificate --certificate fileb://gateway.crt \
  --private-key fileb://gateway.key --certificate-chain fileb://ca.pem \
  --query CertificateArn --output text)"
kubectl -n interlock create secret tls interlock-pg-listener-tls --cert=gateway.crt --key=gateway.key
```

Keep `ca.key` offline or delete it once the certificate is issued, and give
`ca.pem` to every client that connects to the gateway.

## 5. Create the remaining secrets

```bash
kubectl -n interlock create secret generic interlock-redis \
  --from-file=url=<(terraform -chdir="$TF" output -raw cache_url)
kubectl -n interlock create secret generic interlock-admin-secret \
  --from-literal=secret-key="$(openssl rand -hex 32)"
kubectl -n interlock create secret generic interlock-api-key-pepper \
  --from-literal=pepper="$(openssl rand -hex 32)"
```

:::caution
Back up the API-key pepper somewhere safe, for example
`(umask 077; kubectl -n interlock get secret interlock-api-key-pepper -o yaml > pepper-backup.yaml)`
moved to your secrets store. Every agent key is stored as a hash made with it:
if the pepper is lost or changed, every key stops working and every agent has
to be re-keyed.
:::

## 6. Install InterLock

The gateway keeps undelivered audit events on a volume. Create an encrypted
gp3 class for it; the cluster's default class leaves encryption to the
account's EBS default:

```bash
kubectl apply -f - <<'EOF'
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: interlock-gp3
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  encrypted: "true"
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: Delete
allowVolumeExpansion: true
EOF
```

Write the values file. It names Secrets, never credentials. The gateway's
Service becomes a Network Load Balancer that terminates HTTPS on port 3000
with the imported certificate and passes PostgreSQL through on 5432:

```bash
cat > interlock-values.yaml <<EOF
database:
  host: $DB_HOST
  port: 5432
  name: interlock
  existingSecret: interlock-db-credentials
  sslMode: verify-full
  caExistingSecret: interlock-control-db-ca
redis:
  existingSecret: interlock-redis
auth:
  apiKeyPepperExistingSecret: interlock-api-key-pepper
config:
  auditDurabilityMode: strict
gateway:
  replicas: 2
  extraEnv:
    - name: INTERLOCK_DATABASE__MAX_POOL
      value: "4"
  upstreamPostgres:
    host: $DB_HOST
    port: 5432
  pgTls:
    existingSecret: interlock-pg-listener-tls
  service:
    type: LoadBalancer
    annotations:
      service.beta.kubernetes.io/aws-load-balancer-type: nlb
      service.beta.kubernetes.io/aws-load-balancer-ssl-cert: $CERT_ARN
      service.beta.kubernetes.io/aws-load-balancer-ssl-ports: "3000"
      service.beta.kubernetes.io/aws-load-balancer-backend-protocol: tcp
  auditSpool:
    enabled: true
    storageClassName: interlock-gp3
    size: 10Gi
worker:
  replicas: 2
  hpa:
    enabled: false
  extraEnv:
    - name: INTERLOCK_DATABASE__MAX_POOL
      value: "4"
admin:
  replicas: 2
  secret:
    existingSecret: interlock-admin-secret
  extraEnv:
    - name: INTERLOCK_DATABASE__MAX_POOL
      value: "4"
EOF
```

Install the chart; a migration job runs first and creates the schema:

```bash
helm upgrade --install interlock oci://ghcr.io/contextdata/charts/interlock \
  --version "$VERSION" --namespace interlock -f interlock-values.yaml --wait --timeout 15m
kubectl -n interlock get pods
```

To pin the exact artifacts instead of a version, and to check their
signatures first, see [Install a signed release](/operations/deploy-with-helm/#install-a-signed-release).

## 7. Check it and sign in

The load balancer takes a few minutes to start answering after its name
appears:

```bash
export GATEWAY_HOST="$(kubectl -n interlock get service interlock-gateway \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
curl -fsS --cacert ca.pem "https://$GATEWAY_HOST:3000/ready"
```

The response should report `"status": "ready"`, with every check `ok`. Then
open the console through a tunnel that runs until you stop it with Ctrl-C:

```bash
kubectl -n interlock port-forward service/interlock-admin 9090:9090
```

Open `http://127.0.0.1:9090`, sign in as `admin` with the password `admin`,
and choose a new password when asked. The console opens nothing else until
you do.

Agents connect to:

| Protocol | Address |
|---|---|
| MCP | `https://$GATEWAY_HOST:3000/mcp` |
| HTTP proxy | `https://$GATEWAY_HOST:3000/proxy/<source_id>/...` |
| PostgreSQL | `host=$GATEWAY_HOST port=5432 dbname=<source_id> sslmode=verify-full sslrootcert=ca.pem` |

Each client must trust `ca.pem`. `psql` takes it directly through
`sslrootcert`. Settings that replace a client's trust store, rather than add
to it, need the public roots as well, or the client can no longer reach its
model provider; build a bundle once:

```bash
cat /etc/ssl/cert.pem ca.pem > trust-bundle.pem   # Debian and Ubuntu: /etc/ssl/certs/ca-certificates.crt
```

| Client | Trusts the gateway through |
|---|---|
| `psql` and other libpq clients | `sslrootcert=ca.pem` |
| Python clients (the `mcp` SDK, `httpx`, `google-genai`) | `SSL_CERT_FILE=trust-bundle.pem` |
| Codex CLI | `CODEX_CA_CERTIFICATE=trust-bundle.pem` |
| Node.js clients such as Claude Code | `NODE_EXTRA_CA_CERTS=ca.pem` (adds to the built-in roots) |

The load balancer closes TCP connections that are idle for 350 seconds, so
long-lived PostgreSQL clients should use keepalives.

## Next

- [Register a source](/guides/manage-sources/register-a-source/). A source's
  credentials go in a Secret delivered with `gateway.extraEnvFrom`,
  `worker.extraEnvFrom` and `admin.extraEnvFrom`, and are referenced as
  `env://NAME`. A database inside the VPC, such as another RDS instance, has a
  private address, so tick **Allow a private network address** when you
  register it, and verify it against `/run/secrets/control-db-ca/ca.crt`.
- S3 needs no stored keys: InterLock's pods use the AWS credential chain, so
  an [EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html)
  association for the `default` service account in the `interlock` namespace,
  with a role that can read the bucket, is enough. Leave the source's key
  fields empty. Allow a minute after creating the association before
  restarting the pods, which receive the credentials when they start.
- Work through the [production checklist](/operations/production-checklist/).
- [Upgrades and migrations](/operations/upgrades-and-migrations/): an upgrade is
  the same `helm upgrade` with a new `--version`.

## Remove everything

Delete InterLock first, then wait for the load balancer and the volumes to
go: `terraform destroy` cannot delete the VPC while they exist, and anything
left behind keeps being billed. ACM keeps reporting the certificate in use for
a short while after the load balancer is gone, so the certificate is deleted
only once that clears.

Run this in the shell where you set the variables in the first section and in
step 2: `terraform destroy` needs the same `TF_VAR_*` values as `apply`. In a
new shell, set them again first (the manifest is still in
`release-manifest.json`).

```bash
helm uninstall interlock -n interlock
kubectl delete namespace interlock
until [ -z "$(aws elbv2 describe-load-balancers \
  --query "LoadBalancers[?VpcId=='$(terraform -chdir="$TF" output -raw vpc_id)'].LoadBalancerArn" --output text)" ]; do
  sleep 15
done
until [ "$(aws acm describe-certificate --certificate-arn "$CERT_ARN" \
  --query 'length(Certificate.InUseBy)' --output text)" = 0 ]; do
  sleep 15
done
aws acm delete-certificate --certificate-arn "$CERT_ARN"
terraform -chdir="$TF" destroy
```

The state bucket is kept, since it is versioned. Delete it when you no longer
need the state: empty every object version, then `aws s3api delete-bucket
--bucket "$STATE_BUCKET"`.
