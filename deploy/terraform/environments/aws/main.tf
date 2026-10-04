module "cluster" {
  source = "../../modules/aws-eks"

  name                        = var.cluster_name
  kubernetes_version          = var.kubernetes_version
  control_plane_allowed_cidrs = var.control_plane_allowed_cidrs
  node_capacity_type          = var.node_capacity_type
}

# Off by default: the certification run uses an in-cluster data tier. The EKS
# deployment guide turns it on for a managed RDS database and ElastiCache.
module "data_tier" {
  source = "../../modules/aws-data-tier"
  count  = var.managed_data_tier ? 1 : 0

  name                     = var.cluster_name
  vpc_id                   = module.cluster.vpc_id
  subnet_ids               = module.cluster.private_subnet_ids
  client_security_group_id = module.cluster.node_security_group_id
}

module "release" {
  source = "../../modules/release-contract"

  release_version   = var.release_version
  helm_chart_ref    = var.helm_chart_ref
  helm_chart_digest = var.helm_chart_digest
  image_repository  = var.image_repository
  image_digest      = var.image_digest
}
