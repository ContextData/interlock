output "control_db_host" {
  description = "RDS endpoint hostname for the control database."
  value       = aws_db_instance.control.address
}

output "control_db_port" {
  description = "RDS port."
  value       = aws_db_instance.control.port
}

output "control_db_master_secret_arn" {
  description = "Secrets Manager secret holding the RDS master credential."
  value       = aws_db_instance.control.master_user_secret[0].secret_arn
}

output "cache_host" {
  description = "ElastiCache primary endpoint hostname."
  value       = aws_elasticache_replication_group.cache.primary_endpoint_address
}

output "cache_url" {
  description = "rediss:// URL with the cache credential, for the interlock-redis Secret."
  value       = "rediss://:${random_password.cache.result}@${aws_elasticache_replication_group.cache.primary_endpoint_address}:6379/0"
  sensitive   = true
}
