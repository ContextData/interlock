resource "digitalocean_kubernetes_cluster" "this" {
  name    = var.name
  region  = var.region
  version = var.kubernetes_version

  auto_upgrade  = false
  surge_upgrade = false
  ha            = false

  tags = distinct(concat(var.tags, ["interlock", "certification", "disposable", "terraform"]))

  control_plane_firewall {
    enabled           = true
    allowed_addresses = var.control_plane_allowed_cidrs
  }

  node_pool {
    name       = "interlock-runtime"
    size       = var.node_size
    auto_scale = true
    min_nodes  = var.min_nodes
    max_nodes  = var.max_nodes

    labels = {
      "interlock.io/pool" = "runtime"
    }

    tags = ["interlock", "certification", "disposable"]
  }

  lifecycle {
    precondition {
      condition     = var.min_nodes <= var.max_nodes
      error_message = "min_nodes must not exceed max_nodes."
    }
  }
}
