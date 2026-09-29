output "cluster_name" {
  description = "EKS cluster name."
  value       = module.cluster.cluster_name
}

output "cluster_endpoint" {
  description = "EKS API endpoint."
  value       = module.cluster.cluster_endpoint
}

output "kubeconfig_command" {
  description = "Command that obtains short-lived EKS credentials."
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${module.cluster.cluster_name}"
}

output "release_artifacts" {
  description = "Validated immutable release coordinates for deploy-oci-release.sh."
  value       = module.release.release_artifacts
}
