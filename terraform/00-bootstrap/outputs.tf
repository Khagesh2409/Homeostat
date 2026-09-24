# ──────────────────────────────────────────────
# Outputs — useful info printed after terraform apply
# ──────────────────────────────────────────────

output "agent_role_arn" {
  description = "ARN of the agent IAM role — use this when launching the agent's EC2 instance"
  value       = aws_iam_role.agent.arn
}

output "agent_instance_profile_name" {
  description = "Instance profile name — attach this to the agent's EC2 instance"
  value       = aws_iam_instance_profile.agent.name
}

output "watchdog_role_arn" {
  description = "ARN of the watchdog IAM role — use this when launching the watchdog's EC2 instance"
  value       = aws_iam_role.watchdog.arn
}

output "watchdog_instance_profile_name" {
  description = "Instance profile name — attach this to the watchdog's EC2 instance"
  value       = aws_iam_instance_profile.watchdog.name
}

output "sns_topic_arn" {
  description = "SNS topic for alerts — check your email for the confirmation link!"
  value       = aws_sns_topic.alerts.arn
}

output "account_id" {
  description = "Your AWS account ID"
  value       = local.account_id
}
