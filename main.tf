terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# 1. PROVIDER & VARIABLES
provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  description = "AWS Region (ap-southeast-1 is Singapore, closest to PH with low latency)"
  default     = "ap-southeast-1"
}

variable "ssh_public_key" {
  description = "Your SSH public key string (e.g., ssh-rsa AAAA... or ssh-ed25519 AAAA...)"
  type        = string
}

variable "domain_name" {
  description = "Domain name for Certbot SSL (e.g., juansource-aws.mooo.com)"
  default     = "juansource-aws.mooo.com"
}

# 2. DATA SOURCE (Dynamically find latest Ubuntu 22.04 LTS x86_64 AMI)
data "aws_ami" "ubuntu_22_04" {
  most_recent = true
  owners      = ["099720109477"] # Canonical's official AWS Owner ID

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# 3. NETWORKING (VPC, Public Subnet, Internet Gateway, Route Table)
resource "aws_vpc" "portfolio_vpc" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "juansource-vpc"
  }
}

resource "aws_internet_gateway" "portfolio_igw" {
  vpc_id = aws_vpc.portfolio_vpc.id

  tags = {
    Name = "juansource-igw"
  }
}

resource "aws_subnet" "portfolio_subnet" {
  vpc_id                  = aws_vpc.portfolio_vpc.id
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = true

  tags = {
    Name = "juansource-public-subnet"
  }
}

resource "aws_route_table" "portfolio_rt" {
  vpc_id = aws_vpc.portfolio_vpc.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.portfolio_igw.id
  }

  tags = {
    Name = "juansource-rt"
  }
}

resource "aws_route_table_association" "portfolio_rta" {
  subnet_id      = aws_subnet.portfolio_subnet.id
  route_table_id = aws_route_table.portfolio_rt.id
}

# 4. SECURITY GROUP (Ports 22, 80, 443)
resource "aws_security_group" "portfolio_sg" {
  name        = "juansource-sg"
  description = "Allow SSH, HTTP, and HTTPS inbound traffic"
  vpc_id      = aws_vpc.portfolio_vpc.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "juansource-sg"
  }
}

# 5. SSH KEY PAIR
resource "aws_key_pair" "deployer_key" {
  key_name   = "juansource-deployer-key"
  public_key = var.ssh_public_key
}

# 6. BOOTSTRAP SCRIPT (2GB Swap + Docker + Host Nginx + Certbot Helper)
locals {
  aws_bootstrap_user_data = <<-EOF
    #!/bin/bash
    set -euxo pipefail
    export DEBIAN_FRONTEND=noninteractive

    # 1. Ensure 2GB swap exists and is active on every boot (vital for 1GB t3.micro).
    if [ ! -f /swapfile ]; then
      fallocate -l 2G /swapfile
      chmod 600 /swapfile
      mkswap /swapfile
    fi
    swapon --show=NAME | grep -q '^/swapfile$' || swapon /swapfile
    grep -q '^/swapfile none swap sw 0 0$' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    sysctl -w vm.swappiness=10
    grep -q '^vm.swappiness=10$' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf

    # 2. Install runtime dependencies (including docker-compose-v2 plugin).
    apt-get update -y
    apt-get install -y docker.io docker-compose-v2 docker-compose git nginx certbot python3-certbot-nginx curl

    # 3. Enable core services and docker access for ubuntu user.
    systemctl enable --now docker
    systemctl enable --now nginx
    usermod -aG docker ubuntu

    # 4. Configure host Nginx -> JuanSource internal Nginx container (127.0.0.1:3000).
    cat >/etc/nginx/sites-available/juansource.conf <<'NGINXCONF'
    map $http_upgrade $connection_upgrade {
        default upgrade;
        '' close;
    }

    server {
        listen 80 default_server;
        server_name ${var.domain_name} _;

        location / {
            proxy_pass http://127.0.0.1:3000;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
        }
    }
    NGINXCONF

    rm -f /etc/nginx/sites-enabled/default
    ln -sf /etc/nginx/sites-available/juansource.conf /etc/nginx/sites-enabled/juansource.conf
    nginx -t
    systemctl reload nginx

    # 5. Certbot helper script (run after DNS points to this EC2 IP).
    cat >/usr/local/bin/issue-certificates.sh <<'CERTBOT'
    #!/bin/bash
    set -euo pipefail
    certbot --nginx --redirect -d ${var.domain_name}
    CERTBOT
    chmod +x /usr/local/bin/issue-certificates.sh

    # 6. Write quick diagnostics to aid first-boot validation.
    swapon --show > /var/log/swap-status.log
    free -h > /var/log/memory-status.log
  EOF
}

# 7. EC2 INSTANCE (t3.micro Free Tier)
resource "aws_instance" "juansource_server" {
  ami                    = data.aws_ami.ubuntu_22_04.id
  instance_type          = "t3.micro"
  subnet_id              = aws_subnet.portfolio_subnet.id
  vpc_security_group_ids = [aws_security_group.portfolio_sg.id]
  key_name               = aws_key_pair.deployer_key.key_name
  user_data              = local.aws_bootstrap_user_data

  root_block_device {
    volume_size           = 25 # Free tier allows up to 30GB total EBS
    volume_type           = "gp3"
    delete_on_termination = true
  }

  tags = {
    Name = "juansource-aws-server"
  }
}

# 8. OUTPUTS
output "aws_server_public_ip" {
  description = "Public IPv4 of the AWS EC2 instance (Put this in GitHub Secret AWS_HOST_IP)"
  value       = aws_instance.juansource_server.public_ip
}