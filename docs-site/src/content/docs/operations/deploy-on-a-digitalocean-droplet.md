---
title: Deploy on a DigitalOcean Droplet
description: Run InterLock for evaluation on one DigitalOcean Droplet with Docker Compose, reached through an SSH tunnel.
sidebar:
  order: 3
---

The quickest way to try InterLock on DigitalOcean: one Droplet runs the same
Docker Compose stack as the [quick start](/get-started/quick-start/),
including the sample shop database. Nothing is exposed to the internet. Every
port listens on the Droplet's `127.0.0.1` only, a cloud firewall admits SSH
alone, and you reach the console and the gateway through an SSH tunnel.

Plan on about fifteen minutes, most of it the Droplet building the images.
The `s-2vcpu-4gb` size ($24 a month, billed by the hour) builds and runs the
whole stack with room to spare. Delete the Droplet when you are done.

:::caution[For evaluation only]
This stack runs in development mode with development database credentials,
which is safe only because nothing can reach it except through your tunnel.
Do not open its ports. For a deployment a team or real agents will use, follow
[Deploy on DigitalOcean Kubernetes](/operations/deploy-on-digitalocean-kubernetes/).
:::

## What you need

- A DigitalOcean account and [`doctl`](https://docs.digitalocean.com/reference/doctl/how-to/install/),
  signed in with `doctl auth init`.
- An SSH key in your DigitalOcean account. `doctl compute ssh-key list` shows
  them; `doctl compute ssh-key import` adds one.
- `psql`, if you want to follow the quick start's PostgreSQL steps.

## 1. Write the setup file

The Droplet configures itself on first boot from this cloud-init file. It
installs Docker from Ubuntu's packages, checks out an InterLock release,
generates a private signing key for console sessions, and starts the stack:

```bash
cat > interlock-cloud-init.yaml <<'EOF'
#cloud-config
package_update: true
packages: [docker.io, docker-compose-v2, git, openssl]
runcmd:
  - git clone --depth 1 --branch v1.0.0-rc.13 https://github.com/ContextData/interlock /opt/interlock
  - bash -c 'cd /opt/interlock && umask 077 && printf "INTERLOCK_ADMIN__SECRET_KEY=%s\n" "$(openssl rand -hex 32)" > .env'
  - bash -c 'cd /opt/interlock && docker compose --profile quickstart up -d --wait > /var/log/interlock-up.log 2>&1'
EOF
```

To install a different release, change `v1.0.0-rc.13` to its tag.

## 2. Allow SSH and nothing else

A cloud firewall applies to every Droplet with its tag. Create the tag first;
the firewall refuses a tag that does not exist yet:

```bash
doctl compute tag create interlock-eval
doctl compute firewall create --name interlock-eval --tag-names interlock-eval \
  --inbound-rules "protocol:tcp,ports:22,address:0.0.0.0/0,address:::/0" \
  --outbound-rules "protocol:tcp,ports:all,address:0.0.0.0/0,address:::/0 protocol:udp,ports:all,address:0.0.0.0/0,address:::/0 protocol:icmp,address:0.0.0.0/0,address:::/0"
```

## 3. Create the Droplet

This uses the first SSH key in your account. To choose another, replace the
`--ssh-keys` value with its ID:

```bash
doctl compute droplet create interlock-eval --region nyc3 --size s-2vcpu-4gb \
  --image ubuntu-24-04-x64 --tag-name interlock-eval \
  --ssh-keys "$(doctl compute ssh-key list --format ID --no-header | head -1)" \
  --user-data-file interlock-cloud-init.yaml --wait
export DROPLET_IP="$(doctl compute droplet get interlock-eval --format PublicIPv4 --no-header)"
```

## 4. Wait for it to finish

The first boot installs Docker, then builds the images, which takes about
seven minutes. This waits for all of it, then lists the services. SSH can take
a minute to come up after the Droplet is created; if the first command is
refused, run it again:

```bash
ssh root@"$DROPLET_IP" cloud-init status --wait
ssh root@"$DROPLET_IP" 'cd /opt/interlock && docker compose --profile quickstart ps'
```

Every service should be running, and the gateway, admin, PostgreSQL, Redis and
sample database healthy. If something failed, the output of the start-up is in
`/var/log/interlock-up.log` on the Droplet, and
`docker compose logs <service>` in `/opt/interlock` shows one service's logs.

## 5. Open the tunnel

This forwards the console, the gateway and the gateway's PostgreSQL port to
the same addresses on your machine, and runs until you stop it with Ctrl-C:

```bash
ssh -N -L 9090:127.0.0.1:9090 -L 3001:127.0.0.1:3001 -L 5434:127.0.0.1:5434 root@"$DROPLET_IP"
```

| On your machine | Is |
|---|---|
| `http://127.0.0.1:9090` | Admin console |
| `http://127.0.0.1:3001` | Gateway, HTTP and MCP |
| `127.0.0.1:5434` | Gateway, PostgreSQL wire |

These are the addresses the quick start uses, so from here on it works
unchanged: continue with [step 2 of the quick start](/get-started/quick-start/#2-sign-in-and-set-a-password),
signing in as `admin` with the password `admin` and choosing a new one. Run
its `curl` and `psql` commands on your machine, in another terminal, while the
tunnel is open.

## Remove everything

```bash
doctl compute droplet delete interlock-eval --force
doctl compute firewall delete "$(doctl compute firewall list --format ID,Name --no-header | awk '$2 == "interlock-eval" {print $1}')" --force
doctl compute tag delete interlock-eval --force
```
