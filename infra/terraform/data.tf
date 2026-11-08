# Postgres (RDS), RabbitMQ (Amazon MQ) and the secrets the app reads.

resource "random_password" "db" {
  length  = 32
  special = false # keeps the connection URL free of characters that need escaping
}

resource "random_password" "mq" {
  length  = 32
  special = false
}

resource "random_password" "jwt" {
  length  = 64
  special = false
}

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "postgres" {
  identifier                   = local.name
  engine                       = "postgres"
  engine_version               = "16"
  instance_class               = var.db_instance_class
  allocated_storage            = 20
  max_allocated_storage        = 100
  storage_encrypted            = true
  db_name                      = "priorauth"
  username                     = "priorauth"
  password                     = random_password.db.result
  db_subnet_group_name         = aws_db_subnet_group.main.name
  vpc_security_group_ids       = [aws_security_group.db.id]
  publicly_accessible          = false
  backup_retention_period      = 7
  deletion_protection          = var.deletion_protection
  skip_final_snapshot          = !var.deletion_protection
  final_snapshot_identifier    = var.deletion_protection ? "${local.name}-final" : null
  performance_insights_enabled = true
  auto_minor_version_upgrade   = true
}

# Amazon MQ for RabbitMQ: the app's queue code runs unchanged (the retry queue and DLQ are declared
# by the app at startup). Swapping to SQS would need a second queue adapter; see the README.
resource "aws_mq_broker" "rabbitmq" {
  broker_name                = local.name
  engine_type                = "RabbitMQ"
  engine_version             = "3.13"
  host_instance_type         = var.mq_instance_type
  deployment_mode            = "SINGLE_INSTANCE"
  publicly_accessible        = false
  subnet_ids                 = [aws_subnet.private[0].id]
  security_groups            = [aws_security_group.mq.id]
  auto_minor_version_upgrade = true

  user {
    username = "priorauth"
    password = random_password.mq.result
  }

  logs {
    general = true
  }
}

locals {
  amqp_host = trimsuffix(trimprefix(aws_mq_broker.rabbitmq.instances[0].endpoints[0], "amqps://"), ":5671")
}

# One secret holds every value the containers need; ECS injects each key as an environment variable.
resource "aws_secretsmanager_secret" "app" {
  name                    = "${local.name}/app"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    DATABASE_URL = "postgresql+psycopg://priorauth:${random_password.db.result}@${aws_db_instance.postgres.address}:5432/priorauth?sslmode=require"
    AMQP_URL     = "amqps://priorauth:${random_password.mq.result}@${local.amqp_host}:5671/%2F"
    JWT_SECRET   = random_password.jwt.result
    DEMO_USERS   = var.demo_users
    API_KEYS     = var.api_keys
  })
}
