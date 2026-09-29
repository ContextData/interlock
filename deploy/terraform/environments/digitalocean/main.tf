module "cluster" {
  source = "../../modules/digitalocean-doks"

  name                        = var.cluster_name
  region                      = var.region
  kubernetes_version          = var.kubernetes_version
  control_plane_allowed_cidrs = var.control_plane_allowed_cidrs
}

module "release" {
  source = "../../modules/release-contract"

  release_version   = var.release_version
  helm_chart_ref    = var.helm_chart_ref
  helm_chart_digest = var.helm_chart_digest
  image_repository  = var.image_repository
  image_digest      = var.image_digest
}
