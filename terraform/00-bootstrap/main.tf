# ──────────────────────────────────────────────
# Homeostat — Step 0.1: IAM Bootstrap
# ──────────────────────────────────────────────
# This creates:
#   1. Agent IAM role (what the AI uses)
#   2. Permission boundary (the "fence" that limits the agent)
#   3. Watchdog IAM role (the safety supervisor)
#   4. AWS Budget with auto-kill
#   5. SNS topic for alerts
# ──────────────────────────────────────────────

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Uncomment after you create the backend (see docs/aws-setup.md step 4)
  # backend "s3" {
  #   bucket         = "homeostat-terraform-state-YOUR_ACCOUNT_ID"
  #   key            = "bootstrap/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "homeostat-terraform-lock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region
}

# ──────────────────────────────────────────────
# Data: get your account ID automatically
# ──────────────────────────────────────────────

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
}

# ──────────────────────────────────────────────
# 1. PERMISSION BOUNDARY — the fence around the agent
# ──────────────────────────────────────────────
# This is the hard limit. Even if the agent role gets extra
# permissions by accident, this boundary blocks dangerous actions.

resource "aws_iam_policy" "agent_boundary" {
  name        = "homeostat-agent-boundary"
  description = "Permission boundary for the Homeostat agent — blocks self-modification and watchdog access"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # ALLOW: everything the agent legitimately needs
        Sid    = "AllowAgentWorkload"
        Effect = "Allow"
        Action = [
          # EC2 — manage the node
          "ec2:Describe*",
          "ec2:RunInstances",
          "ec2:TerminateInstances",
          "ec2:StartInstances",
          "ec2:StopInstances",
          "ec2:CreateTags",
          "ec2:DeleteTags",
          "ec2:AssociateAddress",
          "ec2:DisassociateAddress",
          "ec2:AllocateAddress",
          "ec2:ReleaseAddress",

          # S3 — memory storage
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation",

          # DynamoDB — runbooks and state
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",

          # Bedrock — call DeepSeek
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",

          # CloudWatch — read metrics
          "cloudwatch:GetMetricData",
          "cloudwatch:ListMetrics",
          "cloudwatch:DescribeAlarms",

          # Budgets — check spending
          "budgets:ViewBudget",
          "budgets:DescribeBudget",

          # SSM — read config/secrets
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:GetParametersByPath",

          # ECR — pull container images
          "ecr:GetAuthorizationToken",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchCheckLayerAvailability",

          # STS — assume roles
          "sts:GetCallerIdentity",
          "sts:AssumeRole",
        ]
        Resource = "*"
      },
      {
        # DENY: the agent cannot touch IAM at all
        # This means it can NEVER change permissions — its own or anyone else's
        Sid    = "DenyAllIAM"
        Effect = "Deny"
        Action = [
          "iam:*",
        ]
        Resource = "*"
      },
      {
        # DENY: the agent cannot touch Organizations or account settings
        Sid    = "DenyAccountManagement"
        Effect = "Deny"
        Action = [
          "organizations:*",
          "account:*",
        ]
        Resource = "*"
      },
    ]
  })
}

# ──────────────────────────────────────────────
# 2. AGENT IAM ROLE — what the AI agent uses
# ──────────────────────────────────────────────

resource "aws_iam_role" "agent" {
  name                 = "homeostat-agent"
  permissions_boundary = aws_iam_policy.agent_boundary.arn

  # This role can be assumed by:
  #   - EC2 instances (when the agent runs on a server)
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Project   = "homeostat"
    Component = "agent"
    ManagedBy = "terraform"
  }
}

# The actual permissions for the agent role
# (still limited by the boundary above — belt AND suspenders)
resource "aws_iam_role_policy" "agent_permissions" {
  name = "homeostat-agent-permissions"
  role = aws_iam_role.agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EC2Management"
        Effect = "Allow"
        Action = [
          "ec2:Describe*",
          "ec2:RunInstances",
          "ec2:TerminateInstances",
          "ec2:StartInstances",
          "ec2:StopInstances",
          "ec2:CreateTags",
          "ec2:DeleteTags",
          "ec2:AssociateAddress",
          "ec2:DisassociateAddress",
          "ec2:AllocateAddress",
          "ec2:ReleaseAddress",
        ]
        # IMPORTANT: only instances tagged with "homeostat"
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:ResourceTag/Project" = "homeostat"
          }
        }
      },
      {
        # EC2 Describe doesn't support tag conditions, allow separately
        Sid    = "EC2DescribeAll"
        Effect = "Allow"
        Action = [
          "ec2:Describe*",
        ]
        Resource = "*"
      },
      {
        Sid    = "S3Access"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
        ]
        # Only homeostat buckets
        Resource = [
          "arn:aws:s3:::homeostat-*",
          "arn:aws:s3:::homeostat-*/*",
        ]
      },
      {
        Sid    = "DynamoDBAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ]
        # Only homeostat tables
        Resource = "arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/homeostat-*"
      },
      {
        Sid    = "BedrockAccess"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
        Resource = "*"
      },
      {
        Sid    = "MonitoringReadOnly"
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricData",
          "cloudwatch:ListMetrics",
          "cloudwatch:DescribeAlarms",
          "budgets:ViewBudget",
          "budgets:DescribeBudget",
        ]
        Resource = "*"
      },
      {
        Sid    = "SSMReadOnly"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:GetParametersByPath",
        ]
        Resource = "arn:aws:ssm:${var.aws_region}:${local.account_id}:parameter/homeostat/*"
      },
      {
        Sid    = "ECRPull"
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchCheckLayerAvailability",
        ]
        Resource = "*"
      },
    ]
  })
}

# Instance profile — this is how EC2 uses the role
# (when you launch a server, you attach this profile to it)
resource "aws_iam_instance_profile" "agent" {
  name = "homeostat-agent"
  role = aws_iam_role.agent.name
}

# ──────────────────────────────────────────────
# 3. WATCHDOG IAM ROLE — the safety supervisor
# ──────────────────────────────────────────────

resource "aws_iam_role" "watchdog" {
  name = "homeostat-watchdog"

  # This role can be assumed by EC2 (the watchdog server)
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Project   = "homeostat"
    Component = "watchdog"
    ManagedBy = "terraform"
  }
}

# Watchdog permissions — very narrow, but includes the kill switch
resource "aws_iam_role_policy" "watchdog_permissions" {
  name = "homeostat-watchdog-permissions"
  role = aws_iam_role.watchdog.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # THE KILL SWITCH
        # The watchdog can attach a "deny everything" policy to the agent role,
        # effectively shutting it down
        Sid    = "KillSwitch"
        Effect = "Allow"
        Action = [
          "iam:PutRolePolicy",
          "iam:DeleteRolePolicy",
        ]
        # Can ONLY modify the agent role — nothing else
        Resource = aws_iam_role.agent.arn
      },
      {
        # Read cost data to check if the agent is overspending
        Sid    = "ReadCosts"
        Effect = "Allow"
        Action = [
          "ce:GetCostAndUsage",
          "ce:GetCostForecast",
          "budgets:ViewBudget",
          "budgets:DescribeBudget",
        ]
        Resource = "*"
      },
      {
        # Read CloudWatch to see what the agent is doing
        Sid    = "ReadMetrics"
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricData",
          "cloudwatch:ListMetrics",
          "cloudwatch:DescribeAlarms",
          "logs:GetLogEvents",
          "logs:FilterLogEvents",
        ]
        Resource = "*"
      },
      {
        # Send alert emails when something goes wrong
        Sid    = "SendAlerts"
        Effect = "Allow"
        Action = [
          "sns:Publish",
        ]
        Resource = aws_sns_topic.alerts.arn
      },
    ]
  })
}

# Instance profile for the watchdog server
resource "aws_iam_instance_profile" "watchdog" {
  name = "homeostat-watchdog"
  role = aws_iam_role.watchdog.name
}

# ──────────────────────────────────────────────
# 4. SNS TOPIC — email alerts to you
# ──────────────────────────────────────────────

resource "aws_sns_topic" "alerts" {
  name = "homeostat-alerts"

  tags = {
    Project   = "homeostat"
    ManagedBy = "terraform"
  }
}

# Subscribe your email to the alerts topic
# After terraform apply, you'll get a confirmation email — click the link!
resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ──────────────────────────────────────────────
# 5. AWS BUDGET — automatic spending limit
# ──────────────────────────────────────────────

resource "aws_budgets_budget" "monthly" {
  name         = "homeostat-monthly"
  budget_type  = "COST"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Alert at 50% — just a heads up
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  # Alert at 80% — warning
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  # Alert at 100% — you're over budget
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }
}
