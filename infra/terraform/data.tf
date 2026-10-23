# Secrets and the database. Nothing sensitive is a variable and nothing sensitive is in a container environment block: the task definitions
# name Secrets Manager entries and ECS injects them at start.
#
# The passwords and tokens are EPHEMERAL (they exist only during a run) and are handed to the database and to Secrets Manager through WRITE-ONLY
# arguments, so none of them is stored in the Terraform state. Terraform sends them only when var.secrets_version changes, which is also how you rotate them.

ephemeral "random_password" "db" {
  length  = 32
  special = false # the password is embedded in a URL, so no character that needs escaping
}

ephemeral "random_password" "ingest_token" {
  length  = 40
  special = false
}

ephemeral "random_password" "admin_token" {
  length  = 40
  special = false
}

resource "aws_db_subnet_group" "relay" {
  name       = "${var.name}-db"
  subnet_ids = var.private_subnet_ids
}

resource "aws_db_instance" "relay" {
  identifier     = "${var.name}-db"
  engine         = "postgres"
  engine_version = "17"
  instance_class = var.db_instance_class

  allocated_storage = var.db_allocated_storage_gb
  storage_type      = "gp3"
  storage_encrypted = true

  db_name             = "relay"
  username            = "relay"
  password_wo         = ephemeral.random_password.db.result
  password_wo_version = var.secrets_version
  port                = 5432

  db_subnet_group_name   = aws_db_subnet_group.relay.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = true

  backup_retention_period   = 7
  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = false
  final_snapshot_identifier = "${var.name}-db-final-${var.final_snapshot_suffix}"
  copy_tags_to_snapshot     = true

  auto_minor_version_upgrade = true
  apply_immediately          = false
}

resource "aws_secretsmanager_secret" "database_url" {
  name_prefix = "${var.name}/database-url-"
}

# sslmode=require: RDS PostgreSQL 15 and later refuse unencrypted connections by default, and this makes the client insist on TLS too.
resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id                = aws_secretsmanager_secret.database_url.id
  secret_string_wo         = "postgresql://${aws_db_instance.relay.username}:${ephemeral.random_password.db.result}@${aws_db_instance.relay.address}:${aws_db_instance.relay.port}/${aws_db_instance.relay.db_name}?sslmode=require"
  secret_string_wo_version = var.secrets_version
}

resource "aws_secretsmanager_secret" "ingest_token" {
  name_prefix = "${var.name}/ingest-token-"
}

resource "aws_secretsmanager_secret_version" "ingest_token" {
  secret_id                = aws_secretsmanager_secret.ingest_token.id
  secret_string_wo         = ephemeral.random_password.ingest_token.result
  secret_string_wo_version = var.secrets_version
}

resource "aws_secretsmanager_secret" "admin_token" {
  name_prefix = "${var.name}/admin-token-"
}

resource "aws_secretsmanager_secret_version" "admin_token" {
  secret_id                = aws_secretsmanager_secret.admin_token.id
  secret_string_wo         = ephemeral.random_password.admin_token.result
  secret_string_wo_version = var.secrets_version
}

# The relay's JSON config (which tenants, which sinks). It names environment variables for every vendor key, never the key itself, so the
# config is not secret in the strict sense, but it is kept out of the task definition so it can change without a new revision.
resource "aws_secretsmanager_secret" "relay_config" {
  name_prefix = "${var.name}/relay-config-"
}

resource "aws_secretsmanager_secret_version" "relay_config" {
  secret_id     = aws_secretsmanager_secret.relay_config.id
  secret_string = "{}"

  lifecycle {
    ignore_changes = [secret_string] # set it once with the AWS CLI; Terraform must not put it back to {}
  }
}
