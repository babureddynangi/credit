# infrastructure/terraform/main.tf
# AWS infrastructure for Credit Fraud Platform

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  backend "s3" {
    bucket = "credit-fraud-tfstate"
    key    = "prod/terraform.tfstate"
    region = "us-east-1"
    encrypt = true
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = "CreditFraudPlatform"
      Environment = var.environment
      ManagedBy   = "Terraform"
      Compliance  = "ECOA-FCRA-BSA"
    }
  }
}

# ─── VPC ─────────────────────────────────────────────────────────────────────
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "credit-fraud-vpc"
  cidr = "10.0.0.0/16"

  azs             = ["${var.aws_region}a", "${var.aws_region}b", "${var.aws_region}c"]
  private_subnets = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
  public_subnets  = ["10.0.101.0/24", "10.0.102.0/24", "10.0.103.0/24"]

  enable_nat_gateway   = true
  single_nat_gateway   = false
  enable_dns_hostnames = true
  enable_dns_support   = true
}

# ─── S3 Raw Data Lake ─────────────────────────────────────────────────────────
resource "aws_s3_bucket" "raw_data_lake" {
  bucket = "${var.project_prefix}-raw-data-lake-${var.environment}"
}

resource "aws_s3_bucket_versioning" "raw_data_lake" {
  bucket = aws_s3_bucket.raw_data_lake.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw_data_lake" {
  bucket = aws_s3_bucket.raw_data_lake.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.platform_key.arn
    }
  }
}

resource "aws_s3_bucket_public_access_block" "raw_data_lake" {
  bucket                  = aws_s3_bucket.raw_data_lake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ─── KMS Key ─────────────────────────────────────────────────────────────────
resource "aws_kms_key" "platform_key" {
  description             = "Credit Fraud Platform encryption key"
  deletion_window_in_days = 30
  enable_key_rotation     = true
}

# ─── RDS PostgreSQL (Operational DB + Audit Trail) ───────────────────────────
resource "aws_db_subnet_group" "main" {
  name       = "credit-fraud-db-subnet"
  subnet_ids = module.vpc.private_subnets
}

resource "aws_db_instance" "operational" {
  identifier             = "${var.project_prefix}-operational-${var.environment}"
  engine                 = "postgres"
  engine_version         = "15.4"
  instance_class         = "db.r6g.xlarge"
  allocated_storage      = 100
  max_allocated_storage  = 1000
  storage_encrypted      = true
  kms_key_id             = aws_kms_key.platform_key.arn
  db_name                = "credit_fraud"
  username               = "cfadmin"
  password               = var.db_password
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  backup_retention_period = 35   # FCRA: 35-day minimum audit retention
  deletion_protection    = true
  skip_final_snapshot    = false
  final_snapshot_identifier = "${var.project_prefix}-final-snapshot"
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  performance_insights_enabled    = true
}

# ─── Amazon Neptune (Graph DB) ───────────────────────────────────────────────
resource "aws_neptune_cluster" "fraud_graph" {
  cluster_identifier                  = "${var.project_prefix}-fraud-graph"
  engine                              = "neptune"
  engine_version                      = "1.3.1.0"
  neptune_subnet_group_name           = aws_neptune_subnet_group.main.name
  vpc_security_group_ids              = [aws_security_group.neptune.id]
  storage_encrypted                   = true
  kms_key_arn                         = aws_kms_key.platform_key.arn
  backup_retention_period             = 35
  skip_final_snapshot                 = false
  final_snapshot_identifier           = "${var.project_prefix}-neptune-final"
  iam_database_authentication_enabled = true
  deletion_protection                 = true

  tags = { Purpose = "RelatedPartyFraudGraph" }
}

resource "aws_neptune_cluster_instance" "fraud_graph" {
  count              = 2
  cluster_identifier = aws_neptune_cluster.fraud_graph.id
  instance_class     = "db.r6g.xlarge"
  neptune_subnet_group_name = aws_neptune_subnet_group.main.name
}

resource "aws_neptune_subnet_group" "main" {
  name       = "credit-fraud-neptune-subnet"
  subnet_ids = module.vpc.private_subnets
}

# ─── MSK (Managed Kafka) ─────────────────────────────────────────────────────
resource "aws_msk_cluster" "event_bus" {
  cluster_name           = "${var.project_prefix}-event-bus"
  kafka_version          = "3.5.1"
  number_of_broker_nodes = 3

  broker_node_group_info {
    instance_type   = "kafka.m5.xlarge"
    client_subnets  = module.vpc.private_subnets
    security_groups = [aws_security_group.msk.id]
    storage_info {
      ebs_storage_info { volume_size = 1000 }
    }
  }

  encryption_info {
    encryption_in_transit { client_broker = "TLS" }
    encryption_at_rest { data_volume_kms_key_id = aws_kms_key.platform_key.arn }
  }

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.msk.name
      }
    }
  }
}

# ─── DynamoDB Feature Store ───────────────────────────────────────────────────
resource "aws_dynamodb_table" "feature_store" {
  name           = "${var.project_prefix}-feature-store"
  billing_mode   = "PAY_PER_REQUEST"
  hash_key       = "entity_id"
  range_key      = "feature_version"

  attribute {
    name = "entity_id"
    type = "S"
  }
  attribute {
    name = "feature_version"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.platform_key.arn
  }

  point_in_time_recovery { enabled = true }
  deletion_protection_enabled = true
}

# ─── EKS (Ray Cluster) ───────────────────────────────────────────────────────
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = "${var.project_prefix}-ray-cluster"
  cluster_version = "1.29"
  vpc_id          = module.vpc.vpc_id
  subnet_ids      = module.vpc.private_subnets

  eks_managed_node_groups = {
    ray_workers = {
      instance_types = ["m5.4xlarge"]
      min_size       = 2
      max_size       = 20
      desired_size   = 4
      labels = { role = "ray-worker" }
    }
    ray_head = {
      instance_types = ["m5.2xlarge"]
      min_size       = 1
      max_size       = 1
      desired_size   = 1
      labels = { role = "ray-head" }
    }
  }

  cluster_addons = {
    coredns    = { most_recent = true }
    kube-proxy = { most_recent = true }
    vpc-cni    = { most_recent = true }
  }
}

# ─── CloudWatch Log Groups ────────────────────────────────────────────────────
resource "aws_cloudwatch_log_group" "msk" {
  name              = "/aws/msk/${var.project_prefix}"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.platform_key.arn
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/credit-fraud/api"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.platform_key.arn
}

resource "aws_cloudwatch_log_group" "llm_audit" {
  name              = "/credit-fraud/llm-audit"
  retention_in_days = 2555  # 7 years for FCRA compliance
  kms_key_id        = aws_kms_key.platform_key.arn
}

# ─── Security Groups ─────────────────────────────────────────────────────────
resource "aws_security_group" "rds" {
  name   = "credit-fraud-rds-sg"
  vpc_id = module.vpc.vpc_id
  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = module.vpc.private_subnets_cidr_blocks
  }
}

resource "aws_security_group" "neptune" {
  name   = "credit-fraud-neptune-sg"
  vpc_id = module.vpc.vpc_id
  ingress {
    from_port   = 8182
    to_port     = 8182
    protocol    = "tcp"
    cidr_blocks = module.vpc.private_subnets_cidr_blocks
  }
}

resource "aws_security_group" "msk" {
  name   = "credit-fraud-msk-sg"
  vpc_id = module.vpc.vpc_id
  ingress {
    from_port   = 9094
    to_port     = 9094
    protocol    = "tcp"
    cidr_blocks = module.vpc.private_subnets_cidr_blocks
  }
}

# ─── Outputs ─────────────────────────────────────────────────────────────────
output "neptune_endpoint"    { value = aws_neptune_cluster.fraud_graph.endpoint }
output "rds_endpoint"        { value = aws_db_instance.operational.endpoint }
output "msk_bootstrap"       { value = aws_msk_cluster.event_bus.bootstrap_brokers_tls }
output "eks_cluster_name"    { value = module.eks.cluster_name }
output "raw_data_lake_bucket"{ value = aws_s3_bucket.raw_data_lake.id }
