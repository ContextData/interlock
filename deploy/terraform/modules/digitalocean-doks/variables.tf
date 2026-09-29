variable "name" {
  description = "Disposable DOKS cluster name."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,39}$", var.name))
    error_message = "Cluster name must be 3-40 lowercase alphanumeric or hyphen characters."
  }
}

variable "region" {
  description = "DigitalOcean region slug."
  type        = string
}

variable "kubernetes_version" {
  description = "Explicit DOKS version slug returned by doctl kubernetes options versions."
  type        = string

  validation {
    condition     = var.kubernetes_version != "latest" && length(var.kubernetes_version) > 5
    error_message = "Use an explicit DOKS version slug; latest is not permitted."
  }
}

variable "control_plane_allowed_cidrs" {
  description = "Public operator/runner CIDRs permitted to reach the DOKS API."
  type        = list(string)

  validation {
    condition = (
      length(var.control_plane_allowed_cidrs) > 0 &&
      alltrue([for cidr in var.control_plane_allowed_cidrs : can(cidrhost(cidr, 0))])
    )
    error_message = "At least one valid public control-plane CIDR is required."
  }
}

variable "node_size" {
  description = "DigitalOcean Droplet slug for worker nodes."
  type        = string
  default     = "s-2vcpu-4gb"
}

variable "min_nodes" {
  description = "Minimum autoscaled worker count."
  type        = number
  default     = 1

  validation {
    condition     = var.min_nodes >= 1 && var.min_nodes <= 3
    error_message = "Disposable clusters require 1-3 minimum nodes."
  }
}

variable "max_nodes" {
  description = "Maximum autoscaled worker count."
  type        = number
  default     = 3

  validation {
    condition     = var.max_nodes >= 1 && var.max_nodes <= 6
    error_message = "Disposable clusters permit at most 6 nodes."
  }
}

variable "tags" {
  description = "Additional cluster tags."
  type        = list(string)
  default     = []
}
