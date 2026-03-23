# infrastructure/terraform/variables.tf

variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  default     = "prod"
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Must be dev, staging, or prod."
  }
}

variable "project_prefix" {
  description = "Short prefix for resource naming"
  type        = string
  default     = "cfp"
}

variable "db_password" {
  description = "RDS master password"
  type        = string
  sensitive   = true
}

variable "db_username" {
  description = "RDS master username"
  type        = string
  default     = "cfadmin"
}

variable "ssh_cidr" {
  description = "CIDR block allowed SSH access to EC2"
  type        = string
  default     = "0.0.0.0/0"
}

variable "key_pair_name" {
  description = "EC2 key pair name for SSH access"
  type        = string
  default     = ""
}

variable "repo_url" {
  description = "Git repository URL to clone on EC2"
  type        = string
  default     = "https://github.com/your-org/credit-fraud-platform.git"
}

variable "openai_api_key" {
  description = "OpenAI API key (leave empty to use MOCK_LLM=true)"
  type        = string
  sensitive   = true
  default     = ""
}
