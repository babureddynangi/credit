# Credit Fraud Detection Platform — AWS Free Tier MVP

A US-compliant credit fraud detection platform with graph-based related-party analysis, deterministic rule engine, LLM-assisted case classification, and a human-in-the-loop analyst workbench. Designed to run entirely on the **AWS Free Tier** with mock backends for local development.

## Architecture

```
Loan Application
      │
      ▼
┌─────────────┐    ┌──────────────────┐    ┌─────────────────┐
│  FastAPI     │───▶│  Graph Analyzer  │───▶│  Rule Engine    │
│  (api/main) │    │  (mock / real)   │    │  (rules/engine) │
└─────────────┘    └──────────────────┘    └─────────────────┘
      │                                            │
      ▼                                            ▼
┌─────────────┐    ┌──────────────────┐    ┌─────────────────┐
│  Heuristic  │    │  LLM Agent       │    │  Policy Engine  │
│  Scoring    │───▶│  (OpenAI / mock) │───▶│  (thresholds)   │
└─────────────┘    └──────────────────┘    └─────────────────┘
      │                                            │
      ▼                                            ▼
┌─────────────┐                          ┌─────────────────┐
│  Audit Log  │                          │  React Analyst  │
│  (CW / DB)  │                          │  Workbench      │
└─────────────┘                          └─────────────────┘
```

## AWS Free Tier Stack

| Service | Free Tier Limit | Usage |
|---------|----------------|-------|
| EC2 t2.micro | 750 h/month | API server |
| RDS db.t3.micro | 20 GB, 750 h/month | PostgreSQL case store |
| S3 | 5 GB | React frontend static hosting |
| SQS | 1M requests/month | Case event notifications |
| CloudWatch Logs | 5 GB/month | Audit trail (7-year FCRA retention) |

## Local Development (No AWS Required)

```bash
# 1. Copy env file
cp .env.example .env.local
# Edit .env.local — set OPENAI_API_KEY or leave MOCK_LLM=true

# 2. Start services
docker-compose -f infrastructure/docker/docker-compose.yml up -d

# 3. API available at http://localhost:8000
# 4. Frontend available at http://localhost:3000
# 5. API docs at http://localhost:8000/docs
```

With `MOCK_LLM=true` and `GRAPH_BACKEND=mock` (defaults), the full pipeline runs without any external services or API keys.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MOCK_LLM` | `true` | Use deterministic mock instead of OpenAI |
| `GRAPH_BACKEND` | `mock` | Use in-memory graph instead of Neptune |
| `OPENAI_API_KEY` | — | Required only when `MOCK_LLM=false` |
| `DATABASE_URL` | sqlite fallback | PostgreSQL connection string |
| `SQS_QUEUE_URL` | — | Optional; skipped if not set |
| `AUDIT_LOG_GROUP` | — | CloudWatch log group; skipped if not set |

## Project Structure

```
credit-fraud-platform/
├── api/
│   ├── main.py                  # FastAPI orchestration endpoint
│   └── schemas/models.py        # Pydantic data models
├── graph/
│   └── analyzer.py              # Related-party graph analysis (mock + real)
├── rules/
│   └── engine.py                # Deterministic fraud rule engine
├── llm/
│   └── agents.py                # LLM classification + policy mapping agents
├── governance/
│   ├── audit.py                 # CloudWatch audit logging
│   └── queue.py                 # SQS case event publisher
├── frontend/
│   └── src/App.jsx              # React analyst workbench
├── infrastructure/
│   ├── docker/
│   │   ├── docker-compose.yml   # Local dev stack
│   │   └── init_db.sql          # PostgreSQL schema
│   └── terraform/
│       ├── free_tier.tf         # AWS Free Tier resources
│       └── variables.tf         # Terraform variables
├── tests/
│   ├── test_properties.py       # Property-based tests (Hypothesis)
│   ├── test_rules_engine.py     # Rule engine unit tests
│   ├── test_llm_mock.py         # LLM mock determinism tests
│   └── test_graph_mock.py       # Graph analyzer tests
├── .env.example                 # Environment variable template
└── requirements.txt
```

## Running Tests

```bash
cd credit-fraud-platform
pip install -r requirements.txt
pytest tests/ -v
```

All 25 tests pass, including property-based tests covering:
- SSN hashing (never stored in plain text)
- Rule engine flag invariants (CRITICAL → manual review)
- Model score bounds (all scores in [0.0, 1.0])
- Policy threshold routing (fraud ≥ 0.85 → escalation)
- Graph risk score bounds

## Deploy to AWS Free Tier

```bash
cd infrastructure/terraform
cp terraform.tfvars.example terraform.tfvars
# Fill in: db_username, db_password, key_pair_name, repo_url, openai_api_key

terraform init
terraform apply
```

Outputs: EC2 public IP, RDS endpoint, SQS queue URL, S3 frontend URL.

## US Compliance Coverage

- **ECOA / Reg B** — Adverse action reason codes on every decision
- **FCRA** — 7-year audit log retention via CloudWatch
- **BSA/AML** — Suspicious activity escalation paths
- **NIST AI RMF** — LLM decision audit trail with model version tracking
- **GLBA** — SSN hashed (SHA-256), no PII in logs

## Decision Flow

```
POST /api/v1/evaluate
  → Graph analysis (related parties, household defaults, fund flow)
  → Heuristic ML scoring (PD, fraud, proxy-borrower)
  → Rule engine (CRITICAL / WARNING hits)
  → LLM classification (case type, fraud label, analyst summary)
  → Policy mapping (approve / hold / manual_review / fraud_escalation / decline)
  → Audit log + SQS event
  → Response with full evidence bundle
```
