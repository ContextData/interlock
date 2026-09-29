terraform {
  # 1.11 is the floor for S3-backend native locking (use_lockfile). The older
  # DynamoDB locking arguments were removed in 1.13, so there is no version
  # that supports both.
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.52.0, < 7.0.0"
    }
  }

  # Partial configuration: bucket, key, and region are supplied with
  # -backend-config at init time so no environment detail is committed here.
  #
  # State must be remote even though the cluster is disposable. With local
  # state, a cancelled or timed-out runner loses the only record of what was
  # created, and the EKS cluster, VPC, and NAT gateway survive with nothing
  # able to destroy them.
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      "interlock.io/environment" = "certification"
      "interlock.io/disposable"  = "true"
      "interlock.io/managed-by"  = "terraform"
    }
  }
}
