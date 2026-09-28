variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "bedrock_model_id" {
  description = "Bedrock model ID to store in SSM for the agent to read"
  type        = string
  default     = "anthropic.claude-3-haiku-20240307-v1:0"
}
