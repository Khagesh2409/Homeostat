variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "allowed_ssh_cidr" {
  description = "CIDR block allowed to SSH into the node (default: everywhere)"
  type        = string
  default     = "0.0.0.0/0"
}

variable "allowed_api_cidr" {
  description = "CIDR block allowed to access KubeAPI and monitoring UIs (default: everywhere)"
  type        = string
  default     = "0.0.0.0/0"
}
