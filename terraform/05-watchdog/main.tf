# ──────────────────────────────────────────────────────────────
# Homeostat — Step 3.1: External Watchdog Provisioning
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
    key          = "watchdog/terraform.tfstate"
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
    Component = "watchdog"
  }
}

# Fetch network resources created in 01-network
data "aws_vpc" "main" {
  filter {
    name   = "tag:Name"
    values = ["homeostat-vpc"]
  }
}

data "aws_subnet" "public" {
  filter {
    name   = "tag:Name"
    values = ["homeostat-public"]
  }
}

data "aws_security_group" "node_sg" {
  filter {
    name   = "tag:Name"
    values = ["homeostat-node-sg"]
  }
}

# Fetch watchdog instance profile created in 00-bootstrap
data "aws_iam_instance_profile" "watchdog" {
  name = "homeostat-watchdog"
}

# Fetch latest Ubuntu 22.04 LTS AMI
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }
}

# ──────────────────────────────────────────────────────────────
# Watchdog Security Group
# ──────────────────────────────────────────────────────────────

resource "aws_security_group" "watchdog_sg" {
  name        = "homeostat-watchdog-sg"
  description = "Security group for external safety supervisor (watchdog)"
  vpc_id      = data.aws_vpc.main.id

  # Ingress: Watchdog API (port 8000) from the agent node
  ingress {
    description     = "Watchdog API from Agent Node"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [data.aws_security_group.node_sg.id]
  }

  # Ingress: SSH from admin IP
  ingress {
    description = "SSH from admin"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.admin_ip]
  }

  # Egress: HTTP to Agent Node on port 8080 (health checks)
  egress {
    description     = "Health check probe to Agent Node"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [data.aws_security_group.node_sg.id]
  }

  # Egress: HTTPS to AWS APIs (IAM, Cost Explorer, CloudWatch, SNS)
  egress {
    description = "HTTPS to AWS APIs and package repositories"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, { Name = "homeostat-watchdog-sg" })
}

# ──────────────────────────────────────────────────────────────
# Watchdog EC2 Instance
# ──────────────────────────────────────────────────────────────

resource "aws_instance" "watchdog" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.watchdog_sg.id]
  iam_instance_profile   = data.aws_iam_instance_profile.watchdog.name

  root_block_device {
    volume_size           = 20
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  user_data = <<-EOF
              #!/bin/bash
              set -euo pipefail
              export DEBIAN_FRONTEND=noninteractive

              apt-get update -y
              apt-get install -y python3 python3-pip curl

              # Install uv
              curl -LsSf https://astral.sh/uv/install.sh | sh
              export PATH="/root/.cargo/bin:$PATH"

              echo "Watchdog instance initialized."
              EOF

  tags = merge(local.common_tags, {
    Name = "homeostat-watchdog"
  })
}
