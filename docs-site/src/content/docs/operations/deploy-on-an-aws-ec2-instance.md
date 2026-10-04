---
title: Deploy on an AWS EC2 instance
description: Run InterLock for evaluation on one EC2 instance with Docker Compose, reached through an SSH tunnel.
sidebar:
  order: 5
---

The quickest way to try InterLock on AWS: one EC2 instance runs the same
Docker Compose stack as the [quick start](/get-started/quick-start/),
including the sample shop database. Nothing is exposed to the internet. Every
port listens on the instance's `127.0.0.1` only, its security group admits SSH
from your address alone, and you reach the console and the gateway through an
SSH tunnel.

Plan on about fifteen minutes, most of it the instance building the images.
A `t3.medium` (2 vCPUs, 4 GiB) builds and runs the whole stack. Terminate it
when you are done.

:::caution[For evaluation only]
This stack runs in development mode with development database credentials,
which is safe only because nothing can reach it except through your tunnel.
Do not open its ports. For a deployment a team or real agents will use, follow
[Deploy on Amazon EKS](/operations/deploy-on-amazon-eks/).
:::

## What you need

- An AWS account and the [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html),
  signed in (`aws sts get-caller-identity` answers).
- An SSH key pair on your machine, for example `~/.ssh/id_ed25519.pub`.
- `psql`, if you want to follow the quick start's PostgreSQL steps.

Set the region and a name, and find the address your connections come from:

```bash
export AWS_REGION=us-east-1
export NAME=interlock-eval
export MY_CIDR="$(curl -4 -fsS https://checkip.amazonaws.com)/32"
```

The instance goes in the region's default VPC. Some accounts have none; this
creates one only if it is missing:

```bash
export VPC_ID="$(aws ec2 describe-vpcs --filters Name=is-default,Values=true \
  --query 'Vpcs[0].VpcId' --output text)"
if [ "$VPC_ID" = "None" ]; then
  export VPC_ID="$(aws ec2 create-default-vpc --query 'Vpc.VpcId' --output text)"
fi
```

## 1. Write the setup file

The instance configures itself on first boot from this cloud-init file. It
installs Docker from Ubuntu's packages, checks out an InterLock release,
generates a private signing key for console sessions, and starts the stack:

```bash
cat > interlock-cloud-init.yaml <<'EOF'
#cloud-config
package_update: true
packages: [docker.io, docker-compose-v2, git, openssl]
runcmd:
  - git clone --depth 1 --branch v1.0.0-rc.15 https://github.com/ContextData/interlock /opt/interlock
  - bash -c 'cd /opt/interlock && umask 077 && printf "INTERLOCK_ADMIN__SECRET_KEY=%s\n" "$(openssl rand -hex 32)" > .env'
  - bash -c 'cd /opt/interlock && docker compose --profile quickstart up -d --wait > /var/log/interlock-up.log 2>&1'
EOF
```

To install a different release, change `v1.0.0-rc.15` to its tag.

## 2. Allow SSH from your address only

```bash
export SG_ID="$(aws ec2 create-security-group --group-name "$NAME" \
  --description "InterLock evaluation: SSH from one address" --vpc-id "$VPC_ID" \
  --query GroupId --output text)"
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
  --protocol tcp --port 22 --cidr "$MY_CIDR" > /dev/null
aws ec2 import-key-pair --key-name "$NAME" \
  --public-key-material fileb://"$HOME/.ssh/id_ed25519.pub" > /dev/null
```

If your address changes, add the new one with the same
`authorize-security-group-ingress` command.

## 3. Create the instance

This starts Ubuntu 24.04 (the image ID comes from Canonical's public
parameter, so it is always the current one), with a root volume large enough
to build the images and the instance metadata service limited to session
tokens. The tag is set in a variable first: written inline inside `$( )`,
bash would brace-expand it:

```bash
TAGS="ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]"
export INSTANCE_ID="$(aws ec2 run-instances \
  --image-id resolve:ssm:/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --instance-type t3.medium --key-name "$NAME" --security-group-ids "$SG_ID" \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":40,"VolumeType":"gp3","Encrypted":true}}]' \
  --metadata-options HttpTokens=required \
  --user-data file://interlock-cloud-init.yaml \
  --tag-specifications "$TAGS" \
  --query 'Instances[0].InstanceId' --output text)"
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
export INSTANCE_IP="$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
```

## 4. Wait for it to finish

The first boot installs Docker, then builds the images. This waits for all of
it, then lists the services. SSH can take a minute to come up after the
instance starts; if the first command is refused, run it again:

```bash
ssh ubuntu@"$INSTANCE_IP" cloud-init status --wait
ssh ubuntu@"$INSTANCE_IP" 'cd /opt/interlock && sudo docker compose --profile quickstart ps'
```

Every service should be running, and the gateway, admin, PostgreSQL, Redis and
sample database healthy. If something failed, the output of the start-up is in
`/var/log/interlock-up.log` on the instance, and
`sudo docker compose logs <service>` in `/opt/interlock` shows one service's
logs.

## 5. Open the tunnel

This forwards the console, the gateway and the gateway's PostgreSQL port to
the same addresses on your machine, and runs until you stop it with Ctrl-C:

```bash
ssh -N -L 9090:127.0.0.1:9090 -L 3001:127.0.0.1:3001 -L 5434:127.0.0.1:5434 ubuntu@"$INSTANCE_IP"
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

:::tip[Without SSH]
To open no inbound port at all, attach an instance profile with the
`AmazonSSMManagedInstanceCore` policy, drop the SSH rule, and forward each
port with `aws ssm start-session --document-name AWS-StartPortForwardingSession`
and the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html).
:::

## Remove everything

Terminating the instance deletes its root volume:

```bash
aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" > /dev/null
aws ec2 wait instance-terminated --instance-ids "$INSTANCE_ID"
aws ec2 delete-security-group --group-id "$SG_ID"
aws ec2 delete-key-pair --key-name "$NAME" > /dev/null
```

If you created the default VPC for this, it costs nothing to keep; to remove
it, delete its internet gateway, subnets and the VPC itself in the VPC console.
