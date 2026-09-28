# ──────────────────────────────────────────────────────────────
# Homeostat — Step 1.1: Node Provisioning
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
    key            = "node/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "homeostat-terraform-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region
}

# Fetch the network resources created in 01-network
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

# Fetch latest Ubuntu 22.04 AMI
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }
}

# Create an Elastic IP first so we can bake it into K3s config
resource "aws_eip" "node_eip" {
  domain = "vpc"

  tags = {
    Name      = "homeostat-eip"
    Project   = "homeostat"
    ManagedBy = "terraform"
  }
}

# Create the EC2 instance
resource "aws_instance" "node" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnet.public.id
  vpc_security_group_ids = [data.aws_security_group.node_sg.id]
  iam_instance_profile   = "homeostat-agent" # Created in 00-bootstrap
  key_name               = var.key_name

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }

  # cloud-init script runs on first boot
  user_data = <<-EOF
    #!/bin/bash
    set -e
    
    # 1. Install K3s (tell it about our exact Public EIP for the TLS SAN)
    # This prevents x509 certificate errors when accessing via the EIP.
    curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server --tls-san ${aws_eip.node_eip.public_ip}" sh -
    
    # 2. Wait for K3s to be ready
    sleep 10
    
    # 3. Install Helm
    curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3
    chmod 700 get_helm.sh
    ./get_helm.sh
    
    # 4. Make kubeconfig readable by ubuntu user
    mkdir -p /home/ubuntu/.kube
    cp /etc/rancher/k3s/k3s.yaml /home/ubuntu/.kube/config
    chown -R ubuntu:ubuntu /home/ubuntu/.kube
    
    # 5. Export KUBECONFIG for root
    echo "export KUBECONFIG=/etc/rancher/k3s/k3s.yaml" >> /root/.bashrc
    
    # 6. Replace 127.0.0.1 with our exact Public EIP in the config
    # This makes the config perfectly ready to be downloaded and used externally.
    sed -i "s/127.0.0.1/${aws_eip.node_eip.public_ip}/g" /etc/rancher/k3s/k3s.yaml
    
    # 7. Placeholder for watchdog registration
    INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id || echo "unknown")
    echo "Node $INSTANCE_ID booted" > /var/log/homeostat-boot.log
  EOF

  tags = {
    Name      = "homeostat-node"
    Project   = "homeostat"
    ManagedBy = "terraform"
    Module    = "02-node"
  }
}

# Attach the EIP to the instance
resource "aws_eip_association" "eip_assoc" {
  instance_id   = aws_instance.node.id
  allocation_id = aws_eip.node_eip.id
}
