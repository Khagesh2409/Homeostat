variable "aws_region" {
  description = "AWS region to deploy in"
  type        = string
  default     = "us-east-1"
}

variable "alert_email" {
  description = "Your email address — you'll get budget warnings and watchdog alerts here"
  type        = string
  # No default — Terraform will ask you for this when you run it
}

variable "monthly_budget_usd" {
  description = "Maximum monthly spend in USD. The agent gets disabled if this is exceeded."
  type        = string
  default     = "100"
}
