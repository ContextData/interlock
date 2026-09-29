output "release_artifacts" {
  description = "Validated immutable release coordinates consumed by the deployment script."
  value       = terraform_data.release.output
}
