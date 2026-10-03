# ──────────────────────────────────────────────────────────────
# Homeostat — Step 4.1: ECR Repositories Provisioning
# ──────────────────────────────────────────────────────────────

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket       = "homeostat-terraform-state-975050226684"
    key          = "ecr/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  common_tags = {
    Project   = "homeostat"
    ManagedBy = "terraform"
    Module    = "06-ecr"
  }

  lifecycle_policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images older than 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 2
        description  = "Retain at most 30 tagged release images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v", "sha-", "main-"]
          countType     = "imageCountMoreThan"
          countNumber   = 30
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

# ──────────────────────────────────────────────────────────────
# Homeostat Agent ECR Repository
# ──────────────────────────────────────────────────────────────

resource "aws_ecr_repository" "agent" {
  name                 = "homeostat-agent"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = merge(local.common_tags, {
    Component = "agent"
  })
}

resource "aws_ecr_lifecycle_policy" "agent" {
  repository = aws_ecr_repository.agent.name
  policy     = local.lifecycle_policy
}

# ──────────────────────────────────────────────────────────────
# Homeostat Watchdog ECR Repository
# ──────────────────────────────────────────────────────────────

resource "aws_ecr_repository" "watchdog" {
  name                 = "homeostat-watchdog"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = merge(local.common_tags, {
    Component = "watchdog"
  })
}

resource "aws_ecr_lifecycle_policy" "watchdog" {
  repository = aws_ecr_repository.watchdog.name
  policy     = local.lifecycle_policy
}
