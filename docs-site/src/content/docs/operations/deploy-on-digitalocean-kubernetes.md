---
title: Deploy on DigitalOcean Kubernetes
description: A production deployment on DOKS with Managed PostgreSQL, Managed Valkey, TLS from Let's Encrypt and the signed Helm chart.
sidebar:
  order: 4
---

This is the production path on DigitalOcean, and the one each release is
certified on. It runs InterLock on a DigitalOcean Kubernetes (DOKS) cluster,
keeps its state in Managed PostgreSQL and Managed Valkey, and serves three
hostnames with Let's Encrypt certificates:

| Hostname | Serves | Reached through |
|---|---|---|
| `gateway.example.com` | HTTP proxy and MCP | Traefik ingress, HTTPS |
| `admin.example.com` | Admin console, limited to your IP addresses | Traefik ingress, HTTPS |
| `pg.example.com` | PostgreSQL wire protocol | Its own TCP load balancer, TLS |

It takes about an hour. For a quick evaluation on one machine, use
[a Droplet](/operations/deploy-on-a-digitalocean-droplet/) instead.

## What you need

- A DigitalOcean account and [`doctl`](https://docs.digitalocean.com/reference/doctl/how-to/install/),
  signed in with `doctl auth init`.
- `kubectl` within one minor version of the cluster, `helm` 3.8 or later,
  `psql` and `openssl`.
- A domain whose DNS is hosted on DigitalOcean. cert-manager proves you own it
  through DNS, which is the only way to get a certificate for the PostgreSQL
  hostname, since that hostname serves no HTTP.
- A DigitalOcean API token that cert-manager can use to write DNS records. A
  [custom-scoped token](https://docs.digitalocean.com/reference/api/create-personal-access-token/)
  with only the `domain` scopes is enough.

Set the names used throughout. Everything below reads these variables:

```bash
export REGION=nyc3
export NAME=interlock
export ZONE=example.com                 # a domain on DigitalOcean DNS
export GATEWAY_HOST=gateway.$ZONE
export ADMIN_HOST=admin.$ZONE
export PG_HOST=pg.$ZONE
export ADMIN_CIDR="$(curl -4 -fsS https://ifconfig.me)/32"   # your IPv4 address may open the console
export VERSION=1.0.0-rc.14              # the InterLock release to install
```

## 1. Create the cluster

```bash
doctl kubernetes cluster create "$NAME" --region "$REGION" \
  --node-pool "name=default;size=s-2vcpu-4gb;count=2" --wait
export CLUSTER_ID="$(doctl kubernetes cluster get "$NAME" --format ID --no-header)"
kubectl get nodes
```

Without `--version`, the cluster runs the newest Kubernetes that DOKS offers.
`doctl` adds the cluster to your kubeconfig and makes it the current context.

## 2. Create the databases

InterLock keeps its configuration and audit log in PostgreSQL and uses Valkey
(Redis-compatible) for sessions, rate limits and cache invalidation. Both are
created in the cluster's region:

```bash
doctl databases create "$NAME-pg" --engine pg --version 17 \
  --region "$REGION" --size db-s-1vcpu-2gb --num-nodes 1 --wait > /dev/null
doctl databases create "$NAME-valkey" --engine valkey \
  --region "$REGION" --size db-s-1vcpu-1gb --num-nodes 1 --wait > /dev/null
export PG_ID="$(doctl databases list --format ID,Name --no-header | awk -v n="$NAME-pg" '$2 == n {print $1}')"
export VALKEY_ID="$(doctl databases list --format ID,Name --no-header | awk -v n="$NAME-valkey" '$2 == n {print $1}')"
```

The output of `create` goes to `/dev/null` because it includes connection
strings with the passwords in them.

Size PostgreSQL by connections, not storage. Every InterLock pod keeps a pool
of connections open, up to `INTERLOCK_DATABASE__MAX_POOL` (10 unless set) plus
one it listens on, and a rolling upgrade briefly runs an extra pod. This guide
runs six pods with a pool of 4, which fits the 2 GB plan: measured, it used 22
connections at rest and 36 while every pod restarted at once, against a limit
of 47. The 1 GB plan leaves too few connections, and pods fail to start with `TooManyConnectionsError`. If
you add replicas, keep pods × (pool + 1), plus two pods for upgrades, below the
plan's connection limit (`SHOW max_connections`, less three reserved).

Create InterLock's own database and login. The login gets its own password,
kept in a shell variable rather than printed:

```bash
doctl databases db create "$PG_ID" interlock
doctl databases user create "$PG_ID" interlock > /dev/null
export DB_PASSWORD="$(doctl databases user get "$PG_ID" interlock --format Password --no-header)"
export DB_HOST="$(doctl databases connection "$PG_ID" --format Host --no-header)"
```

InterLock's migrations need two extensions that only the admin user can
install, and the `interlock` login must own its database:

```bash
ADMIN_URI="$(doctl databases connection "$PG_ID" --format URI --no-header)"
psql "${ADMIN_URI/defaultdb/interlock}" -v ON_ERROR_STOP=1 \
  -c 'ALTER DATABASE interlock OWNER TO interlock' \
  -c 'CREATE EXTENSION IF NOT EXISTS pgcrypto' \
  -c 'CREATE EXTENSION IF NOT EXISTS ltree'
unset ADMIN_URI
```

Save the database's CA certificate. InterLock verifies the server certificate
against it (`sslmode: verify-full`):

```bash
doctl databases get-ca "$PG_ID" --format Certificate --no-header > db-ca.crt
```

Now allow only the cluster to reach both databases. A database with no
trusted sources accepts connections from anywhere, so this step matters; do it
last, because it also shuts out your own machine:

```bash
doctl databases firewalls append "$PG_ID" --rule "k8s:$CLUSTER_ID"
doctl databases firewalls append "$VALKEY_ID" --rule "k8s:$CLUSTER_ID"
```

## 3. Create the secrets

The chart reads every credential from a Kubernetes Secret, so the values file
you write later holds none:

```bash
kubectl create namespace interlock
kubectl -n interlock create secret generic interlock-db-credentials \
  --from-literal=username=interlock --from-literal=password="$DB_PASSWORD"
kubectl -n interlock create secret generic interlock-control-db-ca --from-file=ca.crt=db-ca.crt
kubectl -n interlock create secret generic interlock-redis \
  --from-literal=url="$(doctl databases connection "$VALKEY_ID" --format URI --no-header)/0"
kubectl -n interlock create secret generic interlock-admin-secret \
  --from-literal=secret-key="$(openssl rand -hex 32)"
kubectl -n interlock create secret generic interlock-api-key-pepper \
  --from-literal=pepper="$(openssl rand -hex 32)"
unset DB_PASSWORD
```

:::caution
Back up the API-key pepper
(`kubectl -n interlock get secret interlock-api-key-pepper -o yaml`) somewhere
safe. Every agent key is stored as a hash made with it: if the pepper is lost
or changed, every key stops working and every agent has to be re-keyed.
:::

## 4. Install Traefik and cert-manager

Traefik routes HTTPS to the gateway and the console. `externalTrafficPolicy:
Local` keeps the visitor's real address, which the console's IP allow-list
needs:

```bash
helm upgrade --install traefik traefik --repo https://traefik.github.io/charts \
  --namespace traefik --create-namespace \
  --set service.spec.externalTrafficPolicy=Local \
  --set providers.kubernetesIngress.publishedService.enabled=true --wait
```

cert-manager issues and renews the certificates. It proves domain ownership
by writing a DNS record through the DigitalOcean API, using the token from
[What you need](#what-you-need) in `DO_DNS_TOKEN`:

```bash
helm upgrade --install cert-manager cert-manager --repo https://charts.jetstack.io \
  --namespace cert-manager --create-namespace --set crds.enabled=true --wait
kubectl -n cert-manager create secret generic digitalocean-dns --from-literal=access-token="$DO_DNS_TOKEN"

kubectl apply -f - <<EOF
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef:
      name: letsencrypt-account
    solvers:
      - dns01:
          digitalocean:
            tokenSecretRef:
              name: digitalocean-dns
              key: access-token
EOF
```

The gateway loads its PostgreSQL certificate when it starts, so issue that one
now and wait for it before installing InterLock:

```bash
kubectl apply -f - <<EOF
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: interlock-pg-listener
  namespace: interlock
spec:
  secretName: interlock-pg-listener-tls
  dnsNames: ["$PG_HOST"]
  issuerRef:
    kind: ClusterIssuer
    name: letsencrypt
EOF
kubectl -n interlock wait certificate/interlock-pg-listener --for=condition=Ready --timeout=10m
```

## 5. Install InterLock

Write the values file. It names Secrets and hostnames, never credentials:

```bash
cat > interlock-values.yaml <<EOF
database:
  host: $DB_HOST
  port: 25060
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
    port: 25060
  pgTls:
    existingSecret: interlock-pg-listener-tls
  pgService:
    enabled: true
    type: LoadBalancer
    annotations:
      service.beta.kubernetes.io/do-loadbalancer-name: $NAME-pg
  auditSpool:
    enabled: true
    storageClassName: do-block-storage
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
    - name: FORWARDED_ALLOW_IPS
      value: "*"
    - name: INTERLOCK_DATABASE__MAX_POOL
      value: "4"
  ingress:
    enabled: true
    className: traefik
    annotations:
      cert-manager.io/cluster-issuer: letsencrypt
      traefik.ingress.kubernetes.io/router.middlewares: interlock-admin-allowlist@kubernetescrd
    hosts:
      - host: $ADMIN_HOST
        paths: [{path: /, pathType: Prefix}]
    tls:
      - hosts: [$ADMIN_HOST]
        secretName: interlock-admin-tls
ingress:
  enabled: true
  className: traefik
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt
  hosts:
    - host: $GATEWAY_HOST
      paths: [{path: /, pathType: Prefix}]
  tls:
    - hosts: [$GATEWAY_HOST]
      secretName: interlock-gateway-tls
EOF
```

Limit the console to the addresses in `ADMIN_CIDR`. Until you sign in and
change it, the console accepts the default password, so it must not be open to
the internet:

```bash
kubectl apply -f - <<EOF
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: admin-allowlist
  namespace: interlock
spec:
  ipAllowList:
    sourceRange: ["$ADMIN_CIDR"]
EOF
```

Install the chart. A migration job runs first and creates the schema:

```bash
helm upgrade --install interlock oci://ghcr.io/contextdata/charts/interlock \
  --version "$VERSION" --namespace interlock -f interlock-values.yaml --wait --timeout 15m
kubectl -n interlock get pods
```

To pin the exact artifacts instead of a version, and to check their
signatures first, see [Install a signed release](/operations/deploy-with-helm/#install-a-signed-release).

## 6. Point DNS at the load balancers

Two load balancers now exist: Traefik's, for the gateway and the console, and
the gateway's own, for PostgreSQL:

```bash
INGRESS_IP="$(kubectl -n traefik get service traefik -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
PG_IP="$(kubectl -n interlock get service interlock-gateway-pg -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
for record in "${GATEWAY_HOST%.$ZONE}:$INGRESS_IP" "${ADMIN_HOST%.$ZONE}:$INGRESS_IP" "${PG_HOST%.$ZONE}:$PG_IP"; do
  doctl compute domain records create "$ZONE" --record-type A --record-ttl 300 \
    --record-name "${record%%:*}" --record-data "${record##*:}"
done
kubectl -n interlock wait certificate --all --for=condition=Ready --timeout=10m
```

## 7. Check it and sign in

```bash
curl -fsS "https://$GATEWAY_HOST/ready"
```

The response should report `"status": "ready"`, with every check `ok`. Then open
`https://$ADMIN_HOST`, sign in as `admin` with the password `admin`, and choose
a new password when asked. The console opens nothing else until you do.

Agents connect to:

| Protocol | Address |
|---|---|
| MCP | `https://gateway.example.com/mcp` |
| HTTP proxy | `https://gateway.example.com/proxy/<source_id>/...` |
| PostgreSQL | `host=pg.example.com port=5432 dbname=<source_id> sslmode=verify-full` |

The PostgreSQL certificate is from Let's Encrypt, so clients verify it against
the operating system's public roots. With `psql` 16 or later, add
`sslrootcert=system`. Older clients need the path to the system's bundle
instead, such as `sslrootcert=/etc/ssl/cert.pem` on macOS or
`sslrootcert=/etc/ssl/certs/ca-certificates.crt` on Debian and Ubuntu.

## Next

- [Register a source](/guides/manage-sources/register-a-source/). A source's
  credentials go in a Secret delivered with `gateway.extraEnvFrom`,
  `worker.extraEnvFrom` and `admin.extraEnvFrom`, and are referenced as
  `env://NAME`; PostgreSQL sources on this cluster verify against
  `/run/secrets/control-db-ca/ca.crt`.
- Work through the [production checklist](/operations/production-checklist/).
- [Upgrades and migrations](/operations/upgrades-and-migrations/): an upgrade is
  the same `helm upgrade` with a new `--version`.

## Remove everything

```bash
helm uninstall interlock -n interlock
kubectl delete namespace interlock
helm uninstall traefik -n traefik
doctl kubernetes cluster delete "$NAME" --dangerous --force
doctl databases delete "$PG_ID" --force
doctl databases delete "$VALKEY_ID" --force
```

Uninstall the charts before deleting the cluster: that releases the load
balancers and the audit-spool volumes, which would otherwise be left behind and
billed. Then delete the three DNS records:

```bash
for host in "$GATEWAY_HOST" "$ADMIN_HOST" "$PG_HOST"; do
  doctl compute domain records list "$ZONE" --format ID,Type,Name --no-header \
    | awk -v n="${host%.$ZONE}" '$2 == "A" && $3 == n {print $1}' \
    | xargs -r -n1 doctl compute domain records delete "$ZONE" --force
done
```
