variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "EC2 instance type for the K3s node"
  type        = string
  default     = "t3.medium"
}

variable "key_name" {
  description = "Name of the SSH key pair to attach (optional). Must exist in AWS EC2 console."
  type        = string
  default     = null
}
