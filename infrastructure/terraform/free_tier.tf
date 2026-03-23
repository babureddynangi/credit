# infrastructure/terraform/free_tier.tf
# AWS Free Tier MVP infrastructure
# Free tier limits: RDS db.t3.micro (20GB), EC2 t2.micro (750h/mo), SQS (1M req/mo), CW (5GB/mo)

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# ─── Networking ───────────────────────────────────────────────────────────────

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# ─── Security Groups ──────────────────────────────────────────────────────────

resource "aws_security_group" "api" {
  name        = "credit-fraud-api"
  description = "API server — port 8000 inbound"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "rds" {
  name        = "credit-fraud-rds"
  description = "RDS — accessible only from API EC2"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.api.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ─── RDS PostgreSQL (db.t3.micro — free tier 12 months) ──────────────────────

resource "aws_db_instance" "postgres" {
  identifier        = "credit-fraud-db"
  engine            = "postgres"
  engine_version    = "15"
  instance_class    = "db.t3.micro"   # free tier
  allocated_storage = 20              # free tier max
  storage_type      = "gp2"

  db_name  = "credit_fraud"
  username = var.db_username
  password = var.db_password

  vpc_security_group_ids = [aws_security_group.rds.id]
  skip_final_snapshot    = true
  publicly_accessible    = false
  deletion_protection    = false

  tags = { Name = "credit-fraud-db", Tier = "free" }
}

# ─── EC2 t2.micro (free tier 750h/month) ─────────────────────────────────────

data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
}

resource "aws_instance" "api" {
  ami                    = data.aws_ami.amazon_linux.id
  instance_type          = "t2.micro"   # free tier
  vpc_security_group_ids = [aws_security_group.api.id]
  key_name               = var.key_pair_name

  user_data = <<-EOF
    #!/bin/bash
    yum update -y
    yum install -y docker git
    systemctl start docker
    systemctl enable docker
    usermod -aG docker ec2-user

    # Install docker-compose
    curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" \
      -o /usr/local/bin/docker-compose
    chmod +x /usr/local/bin/docker-compose

    # Clone and start the app
    git clone ${var.repo_url} /app
    cd /app/credit-fraud-platform
    cat > .env.local <<ENVEOF
    DATABASE_URL=postgresql://${var.db_username}:${var.db_password}@${aws_db_instance.postgres.address}:5432/credit_fraud
    MOCK_LLM=true
    GRAPH_BACKEND=mock
    OPENAI_API_KEY=${var.openai_api_key}
    AWS_REGION=${var.aws_region}
    SQS_QUEUE_URL=${aws_sqs_queue.cases.url}
    AUDIT_LOG_GROUP=/credit-fraud/audit
    ENVEOF
    docker-compose -f infrastructure/docker/docker-compose.yml up -d api
  EOF

  tags = { Name = "credit-fraud-api", Tier = "free" }
}

# ─── S3 — Frontend Static Hosting ────────────────────────────────────────────

resource "aws_s3_bucket" "frontend" {
  bucket = "credit-fraud-frontend-${var.environment}"
  tags   = { Name = "credit-fraud-frontend", Tier = "free" }
}

resource "aws_s3_bucket_website_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  index_document { suffix = "index.html" }
  error_document { key    = "index.html" }
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.frontend.arn}/*"
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.frontend]
}

# ─── SQS — Case Notifications (1M req/month free) ────────────────────────────

resource "aws_sqs_queue" "cases" {
  name                       = "credit-fraud-cases"
  message_retention_seconds  = 86400   # 1 day
  visibility_timeout_seconds = 30

  tags = { Name = "credit-fraud-cases", Tier = "free" }
}

# ─── CloudWatch Log Groups (5 GB/month free) ─────────────────────────────────

resource "aws_cloudwatch_log_group" "audit" {
  name              = "/credit-fraud/audit"
  retention_in_days = 2557   # 7 years — FCRA compliance
  tags              = { Name = "credit-fraud-audit", Compliance = "FCRA" }
}

resource "aws_cloudwatch_log_group" "llm_audit" {
  name              = "/credit-fraud/llm-audit"
  retention_in_days = 2557
  tags              = { Name = "credit-fraud-llm-audit", Compliance = "NIST-AI-RMF" }
}

# ─── Outputs ─────────────────────────────────────────────────────────────────

output "api_public_ip"       { value = aws_instance.api.public_ip }
output "rds_endpoint"        { value = aws_db_instance.postgres.address }
output "sqs_queue_url"       { value = aws_sqs_queue.cases.url }
output "frontend_bucket_url" { value = aws_s3_bucket_website_configuration.frontend.website_endpoint }
