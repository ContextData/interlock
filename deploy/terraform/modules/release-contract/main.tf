terraform {
  required_version = ">= 1.11.0, < 2.0.0"
}

resource "terraform_data" "release" {
  input = {
    version           = var.release_version
    helm_chart_ref    = var.helm_chart_ref
    helm_chart_digest = var.helm_chart_digest
    image_repository  = var.image_repository
    image_digest      = var.image_digest
  }
}
