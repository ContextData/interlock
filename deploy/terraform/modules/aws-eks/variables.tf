variable "name" {
  description = "Disposable EKS cluster name."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,39}$", var.name))
    error_message = "Cluster name must be 3-40 lowercase alphanumeric or hyphen characters."
  }
}

variable "kubernetes_version" {
  description = "Explicit EKS Kubernetes minor version, for example 1.34."
  type        = string

  validation {
    condition     = can(regex("^1\\.[0-9]{2}$", var.kubernetes_version))
    error_message = "Kubernetes version must be an explicit 1.xx minor version."
  }
}

variable "control_plane_allowed_cidrs" {
  description = "Public operator/runner CIDRs permitted to reach the EKS API."
  type        = list(string)

  validation {
    condition = (
      length(var.control_plane_allowed_cidrs) > 0 &&
      alltrue([for cidr in var.control_plane_allowed_cidrs : can(cidrhost(cidr, 0))])
    )
    error_message = "At least one valid control-plane CIDR is required."
  }
}

variable "vpc_cidr" {
  description = "CIDR for the disposable VPC."
  type        = string
  default     = "10.42.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "VPC CIDR must be valid."
  }
}

variable "instance_types" {
  description = "Allowed EC2 instance types for the managed node group."
  type        = list(string)
  default     = ["m7i.large", "m6i.large"]
}

variable "min_nodes" {
  description = "Minimum managed-node count."
  type        = number
  default     = 1

  validation {
    condition     = var.min_nodes >= 1 && var.min_nodes <= 3
    error_message = "Disposable clusters require 1-3 minimum nodes."
  }
}

variable "max_nodes" {
  description = "Maximum managed-node count."
  type        = number
  default     = 3

  validation {
    condition     = var.max_nodes >= 1 && var.max_nodes <= 6
    error_message = "Disposable clusters permit at most 6 nodes."
  }
}

variable "desired_nodes" {
  description = "Initial managed-node count."
  type        = number
  default     = 2
}

variable "node_capacity_type" {
  description = "Node purchasing option. SPOT suits disposable certification runs; a deployment you keep should use ON_DEMAND, since a Spot interruption can strand a pod whose volume lives in one zone."
  type        = string
  default     = "SPOT"

  validation {
    condition     = contains(["SPOT", "ON_DEMAND"], var.node_capacity_type)
    error_message = "node_capacity_type must be SPOT or ON_DEMAND."
  }
}

variable "tags" {
  description = "Additional resource tags."
  type        = map(string)
  default     = {}
}
