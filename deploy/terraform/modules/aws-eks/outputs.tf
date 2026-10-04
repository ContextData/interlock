output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "EKS API endpoint."
  value       = module.eks.cluster_endpoint
}

output "vpc_id" {
  description = "Disposable VPC ID."
  value       = module.vpc.vpc_id
}

output "private_subnet_ids" {
  description = "Private subnets, for services that run inside the VPC."
  value       = module.vpc.private_subnets
}

output "node_security_group_id" {
  description = "Security group of the worker nodes, for granting them access to other services."
  value       = module.eks.node_security_group_id
}
