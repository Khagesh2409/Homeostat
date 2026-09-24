#!/bin/bash
# ──────────────────────────────────────────────────────────────
# LocalStack init script — runs once on first startup
# Creates the S3 buckets and DynamoDB tables the agent needs
# ──────────────────────────────────────────────────────────────

set -e
echo "→ Initializing LocalStack AWS resources..."

# Use localstack's bundled awslocal CLI
AWS="awslocal"

# ── S3: agent memory bucket ───────────────────
echo "  Creating S3 bucket: homeostat-memory"
$AWS s3 mb s3://homeostat-memory --region us-east-1
$AWS s3api put-bucket-versioning \
  --bucket homeostat-memory \
  --versioning-configuration Status=Enabled

# ── DynamoDB: runbook store ───────────────────
echo "  Creating DynamoDB table: homeostat-runbooks"
$AWS dynamodb create-table \
  --table-name homeostat-runbooks \
  --attribute-definitions \
    AttributeName=failure_signature,AttributeType=S \
    AttributeName=version,AttributeType=N \
  --key-schema \
    AttributeName=failure_signature,KeyType=HASH \
    AttributeName=version,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --region us-east-1

# ── DynamoDB: LangGraph checkpoint state ──────
echo "  Creating DynamoDB table: homeostat-state"
$AWS dynamodb create-table \
  --table-name homeostat-state \
  --attribute-definitions \
    AttributeName=thread_id,AttributeType=S \
    AttributeName=checkpoint_id,AttributeType=S \
  --key-schema \
    AttributeName=thread_id,KeyType=HASH \
    AttributeName=checkpoint_id,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --region us-east-1

# ── DynamoDB: Terraform state lock ───────────
echo "  Creating DynamoDB table: homeostat-terraform-lock"
$AWS dynamodb create-table \
  --table-name homeostat-terraform-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region us-east-1

echo "✅ LocalStack init complete."
