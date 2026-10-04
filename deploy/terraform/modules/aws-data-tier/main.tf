# The control database and cache for an EKS deployment: private, encrypted,
# and reachable only from the cluster's nodes.

resource "aws_security_group" "database" {
  name        = "${var.name}-control-db"
  description = "InterLock control database: PostgreSQL from the cluster nodes only"
  vpc_id      = var.vpc_id
  tags        = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "database_from_nodes" {
  security_group_id            = aws_security_group.database.id
  referenced_security_group_id = var.client_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "PostgreSQL from the cluster nodes"
}

resource "aws_security_group" "cache" {
  name        = "${var.name}-cache"
  description = "InterLock cache: Valkey from the cluster nodes only"
  vpc_id      = var.vpc_id
  tags        = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "cache_from_nodes" {
  security_group_id            = aws_security_group.cache.id
  referenced_security_group_id = var.client_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  description                  = "Valkey from the cluster nodes"
}

resource "aws_db_subnet_group" "control" {
  name       = "${var.name}-control-db"
  subnet_ids = var.subnet_ids
  tags       = var.tags
}

resource "aws_db_instance" "control" {
  identifier     = "${var.name}-control-db"
  engine         = "postgres"
  engine_version = var.postgres_version
  instance_class = var.db_instance_class

  allocated_storage = var.db_allocated_storage
  storage_type      = "gp3"
  storage_encrypted = true

  db_subnet_group_name   = aws_db_subnet_group.control.name
  vpc_security_group_ids = [aws_security_group.database.id]
  publicly_accessible    = false

  # The master credential is generated and held by Secrets Manager, so it never
  # appears in Terraform state or plans. RDS for PostgreSQL 15+ requires TLS.
  username                    = "interlock_admin"
  manage_master_user_password = true
  ca_cert_identifier          = "rds-ca-rsa2048-g1"

  backup_retention_period    = var.backup_retention_days
  auto_minor_version_upgrade = true
  apply_immediately          = true

  # Disposable by default: destroy removes it without a final snapshot.
  deletion_protection = false
  skip_final_snapshot = true

  tags = var.tags
}

resource "aws_elasticache_subnet_group" "cache" {
  name       = "${var.name}-cache"
  subnet_ids = var.subnet_ids
  tags       = var.tags
}

# ElastiCache AUTH tokens allow 16-128 printable characters except @, / and ".
# Unlike the database credential this one is stored in Terraform state, which
# is why the state bucket must be private and encrypted.
resource "random_password" "cache" {
  length  = 64
  special = false
}

resource "aws_elasticache_replication_group" "cache" {
  replication_group_id = "${substr(var.name, 0, 33)}-valkey"
  description          = "InterLock cache for ${var.name}"

  engine         = "valkey"
  engine_version = var.valkey_version
  node_type      = var.cache_node_type
  port           = 6379

  # A single node with cluster mode off: the gateway's client is not
  # cluster-aware.
  num_cache_clusters         = 1
  automatic_failover_enabled = false

  subnet_group_name  = aws_elasticache_subnet_group.cache.name
  security_group_ids = [aws_security_group.cache.id]

  transit_encryption_enabled = true
  transit_encryption_mode    = "required"
  at_rest_encryption_enabled = true
  auth_token                 = random_password.cache.result

  apply_immediately = true
  tags              = var.tags
}
