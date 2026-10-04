data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  availability_zones = slice(data.aws_availability_zones.available.names, 0, 3)
  common_tags = merge(var.tags, {
    "interlock.io/environment" = "certification"
    "interlock.io/disposable"  = "true"
    "interlock.io/managed-by"  = "terraform"
  })
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "6.5.1"

  name = "${var.name}-vpc"
  cidr = var.vpc_cidr
  azs  = local.availability_zones

  private_subnets = [
    for index, _ in local.availability_zones : cidrsubnet(var.vpc_cidr, 4, index)
  ]
  public_subnets = [
    for index, _ in local.availability_zones : cidrsubnet(var.vpc_cidr, 8, index + 48)
  ]

  enable_nat_gateway = true
  single_nat_gateway = true

  public_subnet_tags = {
    "kubernetes.io/role/elb" = "1"
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = "1"
  }

  tags = local.common_tags
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "21.24.0"

  name               = var.name
  kubernetes_version = var.kubernetes_version

  endpoint_private_access      = true
  endpoint_public_access       = true
  endpoint_public_access_cidrs = var.control_plane_allowed_cidrs

  enable_cluster_creator_admin_permissions = true
  enable_irsa                              = true

  addons = {
    coredns                = {}
    eks-pod-identity-agent = { before_compute = true }
    kube-proxy             = {}
    vpc-cni                = { before_compute = true }
    # Without the EBS CSI driver nothing can provision a volume, so the
    # gateway's audit-spool claim stays Pending and the install never
    # completes. The addon's default StorageClass covers charts that name none.
    aws-ebs-csi-driver = {
      pod_identity_association = [{
        role_arn        = aws_iam_role.ebs_csi.arn
        service_account = "ebs-csi-controller-sa"
      }]
      configuration_values = jsonencode({ defaultStorageClass = { enabled = true } })
    }
  }

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  eks_managed_node_groups = {
    interlock = {
      instance_types = var.instance_types
      capacity_type  = var.node_capacity_type
      min_size       = var.min_nodes
      max_size       = var.max_nodes
      desired_size   = var.desired_nodes
      # `disk_size` is ignored with the module's custom launch template, which
      # left nodes on the 20 GiB default; size the root volume here instead.
      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size           = 50
            volume_type           = "gp3"
            encrypted             = true
            delete_on_termination = true
          }
        }
      }

      labels = {
        "interlock.io/pool" = "runtime"
      }
    }
  }

  tags = local.common_tags
}

data "aws_partition" "current" {}

data "aws_iam_policy_document" "ebs_csi_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

# Assumed by the EBS CSI controller through EKS Pod Identity.
resource "aws_iam_role" "ebs_csi" {
  name               = "${var.name}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_trust.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}
