#!/usr/bin/env python3
"""
scripts/generate_synthetic_dataset.py
Generates aws_synthetic_dataset.jsonl for fine-tuning train.py.
Each record is an AWS Solutions Architect Q&A pair in the schema
expected by format_chatml() in train.py.
"""

import json
import random

random.seed(42)

SERVICES = [
    "EC2", "S3", "RDS", "Lambda", "DynamoDB", "ECS", "EKS", "SQS", "SNS",
    "CloudFront", "Route53", "VPC", "IAM", "CloudWatch", "Kinesis",
    "Glue", "Athena", "Redshift", "ElastiCache", "API Gateway",
]

PATTERNS = [
    {
        "problem": "How do I design a highly available web application on AWS?",
        "solution": "Use multi-AZ EC2 instances behind an Application Load Balancer, RDS Multi-AZ for the database, and S3 + CloudFront for static assets.",
        "services": ["EC2", "ALB", "RDS", "S3", "CloudFront"],
        "cost_estimate": "$150-400/month",
        "complexity": "medium",
        "category": "high_availability",
    },
    {
        "problem": "What is the best way to process large files uploaded to S3?",
        "solution": "Trigger a Lambda function on S3 PutObject events. For files >15 min processing, use SQS + ECS Fargate tasks instead.",
        "services": ["S3", "Lambda", "SQS", "ECS"],
        "cost_estimate": "$10-50/month",
        "complexity": "low",
        "category": "event_driven",
    },
    {
        "problem": "How should I store session state for a serverless API?",
        "solution": "Use ElastiCache (Redis) for low-latency session storage, or DynamoDB with TTL for a fully serverless approach.",
        "services": ["ElastiCache", "DynamoDB", "Lambda", "API Gateway"],
        "cost_estimate": "$20-80/month",
        "complexity": "low",
        "category": "serverless",
    },
    {
        "problem": "How do I build a real-time data pipeline on AWS?",
        "solution": "Ingest with Kinesis Data Streams, process with Lambda or Kinesis Data Analytics, store in S3 or DynamoDB, query with Athena.",
        "services": ["Kinesis", "Lambda", "S3", "DynamoDB", "Athena"],
        "cost_estimate": "$200-800/month",
        "complexity": "high",
        "category": "data_pipeline",
    },
    {
        "problem": "What is the recommended way to run containerized microservices on AWS?",
        "solution": "Use ECS Fargate for serverless containers or EKS for Kubernetes. Front with an ALB and use Service Discovery via Cloud Map.",
        "services": ["ECS", "EKS", "ALB", "ECR", "CloudMap"],
        "cost_estimate": "$300-1200/month",
        "complexity": "high",
        "category": "containers",
    },
    {
        "problem": "How do I implement least-privilege IAM policies?",
        "solution": "Use IAM Access Analyzer to identify unused permissions, apply resource-based policies, use condition keys, and enable SCPs in AWS Organizations.",
        "services": ["IAM", "Organizations", "CloudTrail"],
        "cost_estimate": "$0-10/month",
        "complexity": "medium",
        "category": "security",
    },
    {
        "problem": "How can I reduce S3 storage costs for infrequently accessed data?",
        "solution": "Use S3 Intelligent-Tiering or lifecycle policies to transition objects to S3-IA after 30 days and Glacier after 90 days.",
        "services": ["S3"],
        "cost_estimate": "60-80% cost reduction",
        "complexity": "low",
        "category": "cost_optimization",
    },
    {
        "problem": "How do I set up a multi-region active-active architecture?",
        "solution": "Use Route53 latency-based routing, DynamoDB Global Tables, S3 Cross-Region Replication, and deploy identical stacks via CloudFormation StackSets.",
        "services": ["Route53", "DynamoDB", "S3", "CloudFormation"],
        "cost_estimate": "$500-2000/month",
        "complexity": "high",
        "category": "disaster_recovery",
    },
    {
        "problem": "What is the best way to monitor and alert on AWS infrastructure?",
        "solution": "Use CloudWatch Metrics and Alarms for resource monitoring, CloudWatch Logs Insights for log analysis, and SNS for alert notifications.",
        "services": ["CloudWatch", "SNS", "Lambda"],
        "cost_estimate": "$20-100/month",
        "complexity": "low",
        "category": "observability",
    },
    {
        "problem": "How do I securely expose an internal API to external clients?",
        "solution": "Use API Gateway with a custom domain, WAF for protection, Cognito or Lambda authorizers for auth, and VPC Link for private backend integration.",
        "services": ["API Gateway", "WAF", "Cognito", "VPC"],
        "cost_estimate": "$50-300/month",
        "complexity": "medium",
        "category": "api_design",
    },
]

VARIATIONS = [
    "on a tight budget",
    "for a startup",
    "for an enterprise",
    "with minimal operational overhead",
    "with strict compliance requirements",
    "for a high-traffic application",
    "for a batch processing workload",
    "with a focus on security",
    "for a machine learning workload",
    "with multi-region requirements",
]


def generate_record(base: dict, variation: str = "") -> dict:
    problem = base["problem"]
    if variation:
        problem = problem.rstrip("?") + f" {variation}?"
    return {
        "problem": problem,
        "solution": base["solution"],
        "services": base["services"],
        "cost_estimate": base["cost_estimate"],
        "complexity": base["complexity"],
        "category": base["category"],
        "aws_well_architected_pillars": random.sample(
            ["operational_excellence", "security", "reliability",
             "performance_efficiency", "cost_optimization", "sustainability"],
            k=random.randint(2, 4),
        ),
    }


def main(output_file: str = "aws_synthetic_dataset.jsonl", n: int = 200):
    records = []

    # Base records
    for pattern in PATTERNS:
        records.append(generate_record(pattern))

    # Variations
    while len(records) < n:
        base      = random.choice(PATTERNS)
        variation = random.choice(VARIATIONS)
        records.append(generate_record(base, variation))

    random.shuffle(records)

    with open(output_file, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")

    print(f"Generated {len(records)} records → {output_file}")


if __name__ == "__main__":
    main()
