output "agent_repository_url" {
  description = "URL of the Homeostat Agent ECR repository"
  value       = aws_ecr_repository.agent.repository_url
}

output "agent_repository_arn" {
  description = "ARN of the Homeostat Agent ECR repository"
  value       = aws_ecr_repository.agent.arn
}

output "watchdog_repository_url" {
  description = "URL of the Homeostat Watchdog ECR repository"
  value       = aws_ecr_repository.watchdog.repository_url
}

output "watchdog_repository_arn" {
  description = "ARN of the Homeostat Watchdog ECR repository"
  value       = aws_ecr_repository.watchdog.arn
}
