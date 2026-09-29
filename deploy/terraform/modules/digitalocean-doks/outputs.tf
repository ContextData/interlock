output "cluster_id" {
  description = "DOKS cluster ID."
  value       = digitalocean_kubernetes_cluster.this.id
}

output "cluster_name" {
  description = "DOKS cluster name."
  value       = digitalocean_kubernetes_cluster.this.name
}

output "cluster_endpoint" {
  description = "DOKS API endpoint."
  value       = digitalocean_kubernetes_cluster.this.endpoint
}
