variable "helm_chart_ref" {
  description = "OCI chart reference without a tag or digest."
  type        = string

  validation {
    condition = (
      startswith(var.helm_chart_ref, "oci://") &&
      !strcontains(var.helm_chart_ref, "@") &&
      !can(regex(":[^/]+$", var.helm_chart_ref))
    )
    error_message = "Chart references must use the oci:// scheme."
  }
}

variable "helm_chart_digest" {
  description = "Immutable digest of the published InterLock OCI Helm chart."
  type        = string

  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.helm_chart_digest))
    error_message = "Artifact digests must use sha256 followed by 64 lowercase hex characters."
  }
}

variable "image_repository" {
  description = "OCI repository for the InterLock multi-service runtime image."
  type        = string

  validation {
    condition = (
      !strcontains(var.image_repository, "://") &&
      !strcontains(var.image_repository, "@") &&
      !can(regex(":[^/]+$", var.image_repository)) &&
      length(split("/", var.image_repository)) >= 2
    )
    error_message = "Image repository must be an untagged OCI repository name."
  }
}

variable "image_digest" {
  description = "Immutable digest of the InterLock multi-service runtime image."
  type        = string

  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.image_digest))
    error_message = "Artifact digests must use sha256 followed by 64 lowercase hex characters."
  }
}

variable "release_version" {
  description = "SemVer release version without the leading v."
  type        = string

  validation {
    condition = can(regex(
      "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?(\\+[0-9A-Za-z.-]+)?$",
      var.release_version,
    ))
    error_message = "Release version must be valid SemVer without a leading v."
  }
}
