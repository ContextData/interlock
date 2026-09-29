terraform {
  # 1.11 is the floor for S3-backend native locking (use_lockfile). The older
  # DynamoDB locking arguments were removed in 1.13, so there is no version
  # that supports both.
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = ">= 2.95.0, < 3.0.0"
    }
  }

  # Partial configuration: bucket, key, region, and endpoint are supplied with
  # -backend-config at init time so no environment detail is committed here.
  #
  # State must be remote even though the cluster is disposable. With local
  # state, a cancelled or timed-out runner loses the only record of what was
  # created, and the cluster, VPC, and load balancers survive with nothing able
  # to destroy them. Credentials come from AWS_ACCESS_KEY_ID and
  # AWS_SECRET_ACCESS_KEY (a DigitalOcean Spaces key pair), never from here.
  backend "s3" {}
}

provider "digitalocean" {}
