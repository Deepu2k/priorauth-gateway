output "api_url" {
  description = "Public URL of the API (HTTP only in this demo)."
  value       = "http://${aws_lb.api.dns_name}"
}

output "ecr_repository_url" {
  description = "Push the image here: docker push <url>:<tag>"
  value       = aws_ecr_repository.app.repository_url
}

output "database_endpoint" {
  value = aws_db_instance.postgres.address
}

output "broker_console_url" {
  value = aws_mq_broker.rabbitmq.instances[0].console_url
}

output "app_secret_arn" {
  value = aws_secretsmanager_secret.app.arn
}
