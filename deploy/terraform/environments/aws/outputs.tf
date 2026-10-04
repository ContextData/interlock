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

output "vpc_id" {
  description = "VPC holding the cluster."
  value       = module.cluster.vpc_id
}

output "control_db_host" {
  description = "RDS endpoint of the control database, when the managed data tier is on."
  value       = one(module.data_tier[*].control_db_host)
}

output "control_db_master_secret_arn" {
  description = "Secrets Manager secret holding the RDS master credential, when the managed data tier is on."
  value       = one(module.data_tier[*].control_db_master_secret_arn)
}

output "cache_host" {
  description = "ElastiCache endpoint, when the managed data tier is on."
  value       = one(module.data_tier[*].cache_host)
}

output "cache_url" {
  description = "rediss:// URL for the interlock-redis Secret, when the managed data tier is on. Read it with -raw and pipe it; never print it."
  value       = one(module.data_tier[*].cache_url)
  sensitive   = true
}
