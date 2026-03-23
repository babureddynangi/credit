# Credit Labeling / Fraud-Risk AI Platform

A production-grade, US-compliant credit fraud detection platform using Ray, LLM, graph analysis, and human-in-the-loop review.

## Architecture Layers

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Ingestion | Kafka → AWS MSK | Streaming loan/repayment events |
| Storage | S3 + RDS PostgreSQL + DynamoDB | Raw lake, operational DB, feature store |
| Processing | Ray on AWS EKS | Distributed ETL, entity resolution, feature engineering |
| Graph | Amazon Neptune | Related-party link analysis |
| Models | Ray Serve + SageMaker | PD, fraud, proxy-borrower scoring |
| LLM Layer | OpenAI GPT-4o via Ray Serve | Structured case classification + explanation |
| Policy Engine | Python rules + thresholds | Approve / review / hold / escalate |
| Human Review | React dashboard | Analyst workbench with graph + evidence view |
| Governance | CloudWatch + Audit Trail DB | NIST AI RMF compliance, full traceability |

## US Compliance Coverage

- **ECOA / Reg B**: Adverse action reason codes on every decision
- **FCRA**: Credit bureau data usage and consumer rights logging
- **BSA/AML**: Suspicious activity escalation paths
- **UDAAP**: Non-discriminatory model design + disparate impact monitoring
- **NIST AI RMF**: Govern → Map → Measure → Manage lifecycle
- **GLBA**: Data encryption at rest and in transit, access controls

## Quick Start

```bash
# 1. Infrastructure
cd infrastructure/terraform && terraform init && terraform apply

# 2. Services
docker-compose up -d

# 3. Ray cluster
ray up processing/ray_cluster.yaml

# 4. API
cd api && uvicorn main:app --reload

# 5. Frontend
cd frontend && npm install && npm run dev
```

## Project Structure

```
credit-fraud-platform/
├── infrastructure/       # Terraform (AWS) + Docker
├── ingestion/           # Kafka consumers + data validators
├── processing/          # Ray ETL, entity resolution, graph features
├── models/              # Training, serving, registry
├── graph/               # Neptune queries + link analysis
├── rules/               # Deterministic policy engine
├── llm/                 # OpenAI agents + structured output schemas
├── api/                 # FastAPI decision orchestration
├── frontend/            # React analyst workbench
├── governance/          # Audit logging, monitoring, NIST RMF docs
└── docs/                # Architecture, compliance, runbooks
```
