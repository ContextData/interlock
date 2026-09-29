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
  }

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  eks_managed_node_groups = {
    interlock = {
      instance_types = var.instance_types
      capacity_type  = "SPOT"
      min_size       = var.min_nodes
      max_size       = var.max_nodes
      desired_size   = var.desired_nodes
      disk_size      = 50

      labels = {
        "interlock.io/pool" = "runtime"
      }
    }
  }

  tags = local.common_tags
}
