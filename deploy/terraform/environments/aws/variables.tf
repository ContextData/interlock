variable "region" {
  description = "AWS region for the disposable environment."
  type        = string
}

variable "cluster_name" {
  description = "Unique disposable EKS cluster name."
  type        = string
}

variable "kubernetes_version" {
  description = "Explicit EKS Kubernetes minor version."
  type        = string
}

variable "control_plane_allowed_cidrs" {
  description = "Public operator/runner CIDRs permitted to reach the cluster API."
  type        = list(string)
}

variable "release_version" {
  description = "InterLock SemVer release without the leading v."
  type        = string
}

variable "helm_chart_ref" {
  description = "InterLock OCI chart reference without tag or digest."
  type        = string
}

variable "helm_chart_digest" {
  description = "Immutable InterLock OCI chart digest."
  type        = string
}

variable "image_repository" {
  description = "InterLock runtime OCI image repository without tag or digest."
  type        = string
}

variable "image_digest" {
  description = "Immutable InterLock runtime image digest."
  type        = string
}
