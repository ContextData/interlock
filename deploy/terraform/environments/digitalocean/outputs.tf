output "cluster_id" {
  description = "DOKS cluster ID."
  value       = module.cluster.cluster_id
}

output "cluster_name" {
  description = "DOKS cluster name."
  value       = module.cluster.cluster_name
}

output "cluster_endpoint" {
  description = "DOKS API endpoint."
  value       = module.cluster.cluster_endpoint
}

output "kubeconfig_command" {
  description = "Command that obtains short-lived DOKS credentials."
  value       = "doctl kubernetes cluster kubeconfig save ${module.cluster.cluster_name}"
}

output "release_artifacts" {
  description = "Validated immutable release coordinates for deploy-oci-release.sh."
  value       = module.release.release_artifacts
}
