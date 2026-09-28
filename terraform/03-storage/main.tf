# ──────────────────────────────────────────────────────────────
# Homeostat — Step 0.3: External Storage
# ──────────────────────────────────────────────────────────────
# Creates:
#   1. S3 bucket          — agent memory (runbook artifacts, chaos results)
#   2. DynamoDB: homeostat-runbooks  — structured runbook store
#   3. DynamoDB: homeostat-state     — LangGraph checkpoint store
#   4. S3 backend bucket  — Terraform's own state (idempotent: may already exist)
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
    bucket         = "homeostat-terraform-state-REPLACE_WITH_ACCOUNT_ID"
    key            = "storage/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "homeostat-terraform-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  common_tags = {
    Project   = "homeostat"
    ManagedBy = "terraform"
    Module    = "03-storage"
  }
}

# ──────────────────────────────────────────────────────────────
# 1. S3 — Agent Memory Bucket
# ──────────────────────────────────────────────────────────────
# Stores: runbook artifacts, chaos test results, incident logs

resource "aws_s3_bucket" "memory" {
  bucket = "homeostat-memory-${local.account_id}"
  tags   = merge(local.common_tags, { Name = "homeostat-memory" })
}

# Block all public access — this bucket is private, always
resource "aws_s3_bucket_public_access_block" "memory" {
  bucket = aws_s3_bucket.memory.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Encrypt at rest using AWS-managed keys
resource "aws_s3_bucket_server_side_encryption_configuration" "memory" {
  bucket = aws_s3_bucket.memory.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Versioning — lets us recover corrupted runbooks
resource "aws_s3_bucket_versioning" "memory" {
  bucket = aws_s3_bucket.memory.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Lifecycle — expire old object versions after 90 days to keep costs low
resource "aws_s3_bucket_lifecycle_configuration" "memory" {
  bucket = aws_s3_bucket.memory.id

  # Keep only the 5 most recent versions of any object
  rule {
    id     = "expire-old-versions"
    status = "Enabled"

    filter {} # applies to all objects

    noncurrent_version_expiration {
      noncurrent_days           = 90
      newer_noncurrent_versions = 5
    }

    # Clean up incomplete multipart uploads (saves money)
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Automatically delete chaos test results older than 180 days
  rule {
    id     = "expire-chaos-results"
    status = "Enabled"

    filter {
      prefix = "chaos-results/"
    }

    expiration {
      days = 180
    }
  }
}

# ──────────────────────────────────────────────────────────────
# 2. DynamoDB — Runbook Store
# ──────────────────────────────────────────────────────────────
# Schema:
#   PK: failure_signature (string) — structured key, e.g. "pod:CrashLoopBackOff:deployment/nginx:abc123"
#   SK: version (number)           — increments on each update
#
# Additional attributes (not declared here, set by the agent):
#   runbook_json     — the full runbook as JSON
#   created_at       — ISO timestamp
#   last_used        — ISO timestamp
#   times_used       — integer
#   times_succeeded  — integer
#   times_failed     — integer
#   avg_recovery_ms  — float
#   confidence       — float (success_rate weighted by usage)

resource "aws_dynamodb_table" "runbooks" {
  name         = "homeostat-runbooks"
  billing_mode = "PAY_PER_REQUEST" # No capacity planning needed — pay per operation
  hash_key     = "failure_signature"
  range_key    = "version"

  attribute {
    name = "failure_signature"
    type = "S"
  }

  attribute {
    name = "version"
    type = "N"
  }

  # Point-in-time recovery — can restore to any second in the last 35 days
  point_in_time_recovery {
    enabled = true
  }

  # Encrypt at rest
  server_side_encryption {
    enabled = true
  }

  # TTL — automatically expire runbooks that haven't been used in a year
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = merge(local.common_tags, { Name = "homeostat-runbooks" })
}

# ──────────────────────────────────────────────────────────────
# 3. DynamoDB — LangGraph Checkpoint State
# ──────────────────────────────────────────────────────────────
# LangGraph uses this to persist the agent's state between invocations.
# If the agent pod dies mid-incident, it resumes from the last checkpoint.
#
# Schema (LangGraph's format):
#   PK: thread_id     — one "thread" per incident
#   SK: checkpoint_id — sequential checkpoint within that thread

resource "aws_dynamodb_table" "state" {
  name         = "homeostat-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "thread_id"
  range_key    = "checkpoint_id"

  attribute {
    name = "thread_id"
    type = "S"
  }

  attribute {
    name = "checkpoint_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  # Expire checkpoint state after 7 days — incidents are short-lived
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = merge(local.common_tags, { Name = "homeostat-state" })
}

# ──────────────────────────────────────────────────────────────
# 4. SSM Parameters — store config the agent reads at runtime
# ──────────────────────────────────────────────────────────────
# These are safe to store here (non-secret config values).
# Actual secrets (if any) would use SecureString type.

resource "aws_ssm_parameter" "memory_bucket" {
  name  = "/homeostat/s3_bucket"
  type  = "String"
  value = aws_s3_bucket.memory.id

  tags = local.common_tags
}

resource "aws_ssm_parameter" "runbooks_table" {
  name  = "/homeostat/dynamodb/runbooks_table"
  type  = "String"
  value = aws_dynamodb_table.runbooks.name

  tags = local.common_tags
}

resource "aws_ssm_parameter" "state_table" {
  name  = "/homeostat/dynamodb/state_table"
  type  = "String"
  value = aws_dynamodb_table.state.name

  tags = local.common_tags
}

resource "aws_ssm_parameter" "bedrock_model_id" {
  name  = "/homeostat/bedrock/model_id"
  type  = "String"
  value = var.bedrock_model_id

  tags = local.common_tags
}
