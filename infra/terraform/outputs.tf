output "webhook_url" {
  description = "The URL to give HighLevel. With domain_name and route53_zone_id it is your name; otherwise point your own DNS name at load_balancer_dns_name, because a certificate cannot cover an *.elb.amazonaws.com name."
  value       = var.domain_name != "" ? "https://${var.domain_name}/" : "https://<your DNS name for ${aws_lb.relay.dns_name}>/"
}

output "load_balancer_dns_name" {
  description = "The load balancer's own name, for a CNAME or alias record."
  value       = aws_lb.relay.dns_name
}

output "load_balancer_zone_id" {
  description = "Its hosted zone id, needed for a Route 53 alias record outside this stack."
  value       = aws_lb.relay.zone_id
}

output "ecr_repository_url" {
  description = "Push the image here before the first apply of the services."
  value       = aws_ecr_repository.relay.repository_url
}

output "database_endpoint" {
  description = "The RDS address. It is private; only the relay tasks can reach it."
  value       = aws_db_instance.relay.address
}

output "cluster_name" {
  value = aws_ecs_cluster.relay.name
}

output "web_service_name" {
  value = aws_ecs_service.web.name
}

output "worker_service_name" {
  value = aws_ecs_service.worker.name
}

output "ingest_token_secret_arn" {
  description = "Read the ingest token for the HighLevel workflow with `aws secretsmanager get-secret-value --secret-id <this>`."
  value       = aws_secretsmanager_secret.ingest_token.arn
}

output "admin_token_secret_arn" {
  value = aws_secretsmanager_secret.admin_token.arn
}

output "relay_config_secret_arn" {
  description = "Put the relay's JSON config here with `aws secretsmanager put-secret-value`; Terraform leaves it alone afterwards."
  value       = aws_secretsmanager_secret.relay_config.arn
}
