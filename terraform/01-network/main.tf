# ──────────────────────────────────────────────────────────────
# Homeostat — Step 1.1: Network Provisioning
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
    key            = "network/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "homeostat-terraform-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  common_tags = {
    Project   = "homeostat"
    ManagedBy = "terraform"
    Module    = "01-network"
  }
}

# ──────────────────────────────────────────────────────────────
# VPC & Subnet
# ──────────────────────────────────────────────────────────────

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = merge(local.common_tags, { Name = "homeostat-vpc" })
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = true
  availability_zone       = "${var.aws_region}a"
  tags                    = merge(local.common_tags, { Name = "homeostat-public" })
}

# ──────────────────────────────────────────────────────────────
# Internet Gateway & Routing
# ──────────────────────────────────────────────────────────────

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
  tags   = merge(local.common_tags, { Name = "homeostat-igw" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }

  tags = merge(local.common_tags, { Name = "homeostat-public-rt" })
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# ──────────────────────────────────────────────────────────────
# Security Group
# ──────────────────────────────────────────────────────────────

resource "aws_security_group" "node_sg" {
  name        = "homeostat-node-sg"
  description = "Security group for Homeostat K3s node"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.allowed_ssh_cidr]
  }

  ingress {
    description = "KubeAPI"
    from_port   = 6443
    to_port     = 6443
    protocol    = "tcp"
    cidr_blocks = [var.allowed_api_cidr]
  }

  ingress {
    description = "Prometheus UI"
    from_port   = 9090
    to_port     = 9090
    protocol    = "tcp"
    cidr_blocks = [var.allowed_api_cidr]
  }

  ingress {
    description = "Alertmanager UI"
    from_port   = 9093
    to_port     = 9093
    protocol    = "tcp"
    cidr_blocks = [var.allowed_api_cidr]
  }
  
  ingress {
    description = "Agent webhook (NodePort)"
    from_port   = 30080
    to_port     = 30080
    protocol    = "tcp"
    cidr_blocks = [var.allowed_api_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, { Name = "homeostat-node-sg" })
}
