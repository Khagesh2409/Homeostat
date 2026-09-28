output "memory_bucket_name" {
  description = "S3 bucket name for agent memory"
  value       = aws_s3_bucket.memory.id
}

output "memory_bucket_arn" {
  description = "S3 bucket ARN"
  value       = aws_s3_bucket.memory.arn
}

output "runbooks_table_name" {
  description = "DynamoDB table name for runbooks"
  value       = aws_dynamodb_table.runbooks.name
}

output "runbooks_table_arn" {
  description = "DynamoDB table ARN for runbooks"
  value       = aws_dynamodb_table.runbooks.arn
}

output "state_table_name" {
  description = "DynamoDB table name for LangGraph state"
  value       = aws_dynamodb_table.state.name
}

output "state_table_arn" {
  description = "DynamoDB table ARN for LangGraph state"
  value       = aws_dynamodb_table.state.arn
}

output "ssm_prefix" {
  description = "SSM parameter prefix — agent reads config from here"
  value       = "/homeostat/"
}
