variable "aws_region" {
  description = "AWS region to deploy the watchdog server"
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "EC2 instance type for the watchdog server"
  type        = string
  default     = "t3.nano"
}

variable "admin_ip" {
  description = "Your IP address for SSH access to the watchdog instance (CIDR notation, e.g. 1.2.3.4/32)"
  type        = string
  default     = "0.0.0.0/0"
}
