variable "name" {
  description = "Prefix for every data-tier resource."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,39}$", var.name))
    error_message = "name must be 3-40 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "vpc_id" {
  description = "VPC that holds the cluster."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets for the database and cache."
  type        = list(string)
}

variable "client_security_group_id" {
  description = "Security group allowed to reach the database and cache: the cluster's nodes."
  type        = string
}

variable "postgres_version" {
  description = "PostgreSQL major version for the control database."
  type        = string
  default     = "17"
}

variable "db_instance_class" {
  description = "RDS instance class. Size max_connections for your pod count and pool size."
  type        = string
  default     = "db.t4g.small"
}

variable "db_allocated_storage" {
  description = "Initial storage in GiB."
  type        = number
  default     = 20
}

variable "backup_retention_days" {
  description = "Automated backup retention. Raise it for anything you keep."
  type        = number
  default     = 1
}

variable "valkey_version" {
  description = "Valkey engine version for the cache."
  type        = string
  default     = "8.2"
}

variable "cache_node_type" {
  description = "ElastiCache node type."
  type        = string
  default     = "cache.t4g.micro"
}

variable "tags" {
  description = "Tags added to every resource."
  type        = map(string)
  default     = {}
}
