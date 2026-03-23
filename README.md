# Credit Fraud Detection Platform - AWS Free Tier MVP

A US-compliant credit fraud detection platform with graph-based related-party analysis,
deterministic rule engine, LLM-assisted case classification, and a human-in-the-loop
analyst workbench. Runs entirely on the AWS Free Tier with mock backends for local dev.

## Pipeline

Every loan application passes through 7 stages in sequence:

```
[1] Graph Analysis       - related-party network, household defaults, fund flow
[2] Heuristic ML Scoring - PD score, fraud score, proxy-borrower score
[3] Rule Engine          - 12 deterministic rules (CRITICAL / WARNING hits)
[4] Intake Validation    - evidence bundle completeness check
[5] LLM Classification   - case type, fraud label, analyst summary (OpenAI / mock)
[6] Policy Mapping       - operational status, SLA priority, adverse action codes
[7] Audit Log            - CloudWatch + PostgreSQL (FCRA 7-year retention)
```

## AWS Free Tier Stack

| Service         | Free Tier Limit      | Usage                         |
|-----------------|----------------------|-------------------------------|
| EC2 t2.micro    | 750 h/month          | API server                    |
| RDS db.t3.micro | 20 GB, 750 h/month   | PostgreSQL case store         |
| S3              | 5 GB                 | React frontend static hosting |
| SQS             | 1M requests/month    | Case event notifications      |
| CloudWatch Logs | 5 GB/month           | Audit trail (7-year FCRA)     |

## Sample Run Results (100,000 Applications)

Validated on a synthetic dataset of 100k loan applications (~$3B portfolio):

| Metric                   | Value           |
|--------------------------|-----------------|
| Applications processed   | 100,000         |
| Throughput               | ~1,430 apps/sec |
| Total loan volume        | $2,998,868,877  |
| Approved                 | 85,052 (85.1%)  |
| Manual review            | 9,929  (9.9%)   |
| Fraud escalation         | 5,019  (5.0%)   |
| Total rule hits          | 40,257          |
| Critical rule hits       | 12,421          |
| Review loan exposure     | $787M  (26.2%)  |
| Fraud escalated exposure | $364M  (12.1%)  |

Fraud label breakdown:

| Label                       | Count  | Pct   |
|-----------------------------|--------|-------|
| none (clean)                | 85,052 | 85.1% |
| related_party_risk          | 7,940  | 7.9%  |
| suspected_proxy_borrower    | 3,999  | 4.0%  |
| suspected_coordinated_fraud | 1,968  | 2.0%  |
| suspected_identity_misuse   | 1,041  | 1.0%  |

Priority SLA distribution:

| Priority                | SLA | Cases  | Pct   |
|-------------------------|-----|--------|-------|
| 10 - Fraud escalation   | 4h  | 5,019  | 5.0%  |
| 25 - Critical rule hit  | 12h | 9,903  | 9.9%  |
| 50 - Standard           | 48h | 85,052 | 85.1% |

## Local Development (No AWS Required)

```bash
# 1. Copy env file
cp .env.example .env.local

# 2. Start services
docker-compose -f infrastructure/docker/docker-compose.yml up -d

# 3. API at http://localhost:8000
# 4. Frontend at http://localhost:3000
# 5. API docs at http://localhost:8000/docs
```

With MOCK_LLM=true and GRAPH_BACKEND=mock (defaults), the full 7-stage pipeline
runs without any external services or API keys.

## Run Sample Data

```bash
cd credit-fraud-platform
python scripts/run_sample_data.py
```

Generates 100,000 synthetic applications through the full pipeline and writes:
- results/sample_run_results.csv  - 50-column per-application dataset
- results/sample_run_summary.json - aggregate stats
- results/sample_run_report.txt   - human-readable report

## Environment Variables

| Variable        | Default | Description                              |
|-----------------|---------|------------------------------------------|
| MOCK_LLM        | true    | Use deterministic mock instead of OpenAI |
| GRAPH_BACKEND   | mock    | Use in-memory graph instead of Neptune   |
| OPENAI_API_KEY  | -       | Required only when MOCK_LLM=false        |
| DATABASE_URL    | -       | PostgreSQL connection string             |
| SQS_QUEUE_URL   | -       | Optional; skipped if not set             |
| AUDIT_LOG_GROUP | -       | CloudWatch log group; skipped if not set |

## Project Structure

```
credit-fraud-platform/
 api/
    main.py                  # FastAPI orchestration (7-stage pipeline)
    schemas/models.py        # Pydantic data models
 graph/
    analyzer.py              # Related-party graph (mock + Neo4j + Neptune)
 rules/
    engine.py                # 12 deterministic fraud rules
 llm/
    agents.py                # LLM classification + policy mapping agents
 governance/
    audit.py                 # CloudWatch + PostgreSQL audit logging
    queue.py                 # SQS case event publisher
 frontend/
    src/App.jsx              # React analyst workbench
 scripts/
    run_sample_data.py       # 100k sample data runner
 infrastructure/
    docker/
       docker-compose.yml   # Local dev stack
       init_db.sql          # PostgreSQL schema
    terraform/
        free_tier.tf         # AWS Free Tier resources
        variables.tf
 tests/
    test_properties.py       # Property-based tests (Hypothesis)
    test_rules_engine.py     # Rule engine unit tests
    test_llm_mock.py         # LLM mock determinism tests
    test_graph_mock.py       # Graph analyzer tests
 .env.example
 requirements.txt
```

## Running Tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

25 tests pass including property-based tests covering SSN hashing, rule engine
invariants, model score bounds, policy threshold routing, and graph risk bounds.

## Deploy to AWS Free Tier

```bash
cd infrastructure/terraform
terraform init
terraform apply
```

Outputs: EC2 public IP, RDS endpoint, SQS queue URL, S3 frontend URL.

## Rule Engine

| Rule | Severity | Signal                                        |
|------|----------|-----------------------------------------------|
| R001 | WARNING  | Household member defaulted within 90 days     |
| R002 | CRITICAL | Shared bank account with defaulter            |
| R003 | CRITICAL | Funds transferred to related defaulter in 7d  |
| R004 | CRITICAL | Device fingerprint in fraud registry          |
| R005 | CRITICAL | Phone controlled by another borrower          |
| R006 | WARNING  | Loan purpose vs transaction mismatch          |
| R007 | CRITICAL | 3+ household defaults (fraud ring signal)     |
| R008 | CRITICAL | Shared device with recent defaulter           |
| R009 | CRITICAL | Full loan transferred out within 24h          |
| R010 | WARNING  | High-density related-party cluster            |
| R011 | WARNING  | Declared income 2.5x bureau estimate          |
| R012 | WARNING  | Address changed within 30 days of application |

## US Compliance Coverage

- ECOA / Reg B - Adverse action reason codes on every non-approval decision
- FCRA         - 7-year audit log retention via CloudWatch
- BSA/AML      - Suspicious activity escalation paths
- NIST AI RMF  - LLM decision audit trail with model version and prompt hash
- GLBA         - SSN hashed (SHA-256), no PII in logs
- UDAAP        - Disparate impact monitoring hook in ModelMonitor

## Decision Flow

```
POST /v1/applications/evaluate
  -> Graph analysis (related parties, household defaults, fund flow)
  -> Heuristic ML scoring (PD, fraud, proxy-borrower)
  -> Rule engine (CRITICAL / WARNING hits)
  -> LLM classification (case type, fraud label, analyst summary)
  -> Policy mapping (approve / hold / manual_review / fraud_escalation / decline)
  -> Audit log + SQS event
  -> DecisionOutput (status, scores, rule hits, adverse action codes, explanation)
```
