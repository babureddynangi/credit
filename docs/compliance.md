# docs/compliance.md
# US Regulatory Compliance Guide — Credit Fraud AI Platform

## Overview

This platform is designed for use by US-regulated lending institutions and must
comply with the following federal laws and frameworks. This document maps each
regulation to specific platform controls.

---

## 1. Equal Credit Opportunity Act (ECOA) / Regulation B

**Key requirements:**
- No discrimination based on race, color, religion, national origin, sex, marital status,
  age, or receipt of public assistance
- Adverse action notices with specific reason codes within 30 days
- No use of protected class information in credit decisions

**Platform controls:**

| Control | Implementation |
|---------|---------------|
| Protected class exclusion | LLM system prompt explicitly prohibits mentioning protected characteristics. Feature engineering excludes demographic proxies. |
| Adverse action reason codes | `adverse_action_codes` field in every DecisionOutput. Mapped to CFPB standard codes (1–25) plus internal fraud codes. |
| Adverse action notices | `adverse_action_notices` DB table stores all notices with delivery confirmation. |
| Disparate impact monitoring | `ModelMonitor.check_disparate_impact()` runs 4/5 rule checks monthly. Alerts stored in `model_monitoring` table. |
| ECOA-compliant explanations | `explanation` field in DecisionOutput: plain English, no protected class references. |

**Verification checklist:**
- [ ] Run disparate impact analysis monthly across all protected classes
- [ ] Review LLM outputs for unintentional proxy variable use quarterly
- [ ] Adverse action notices delivered within 30 days for all denials
- [ ] Model documentation includes demographic parity metrics

---

## 2. Fair Credit Reporting Act (FCRA)

**Key requirements:**
- Consumer disclosure rights
- Data accuracy obligations for bureau data
- 7-year retention for adverse action records
- Permissible purpose for credit pulls

**Platform controls:**

| Control | Implementation |
|---------|---------------|
| 7-year retention | CloudWatch log group `/credit-fraud/audit` retention = 2555 days (7 years). PostgreSQL audit_trail table never deletes. |
| Bureau data accuracy | `bureau_summary` in evidence bundle traces all bureau data used. |
| Permissible purpose logging | Every credit pull logged in audit_trail with `action = "bureau_pull"` and stated permissible purpose. |
| Consumer dispute handling | Cases can be reopened via `PUT /v1/cases/{case_id}/reopen` with dispute flag. |
| Data lineage | `model_scores` table tracks model version + features for every scored application. |

---

## 3. Bank Secrecy Act (BSA) / Anti-Money Laundering (AML)

**Key requirements:**
- Suspicious Activity Report (SAR) filing for suspected fraud/money laundering
- Customer Due Diligence (CDD)
- Beneficial owner identification

**Platform controls:**

| Control | Implementation |
|---------|---------------|
| SAR triggers | `fraud_escalation` operational status triggers SAR workflow. Cases with `confirmed_fraud` label automatically queued for BSA officer review. |
| CDD documentation | KYC data collected at intake; entity resolution links beneficial owners via graph. |
| Beneficial owner detection | Proxy borrower model specifically detects non-named beneficiaries. Fund flow graph traces disbursement destinations. |
| SAR retention | Fraud escalation audit records retained 5+ years in CloudWatch. |
| Coordinator detection | `suspected_coordinated_fraud` case type triggers ring analysis in graph layer. |

---

## 4. Unfair, Deceptive, or Abusive Acts or Practices (UDAAP)

**Key requirements:**
- Credit decisions must be based on legitimate, non-discriminatory factors
- Practices must not cause substantial consumer harm
- Abusive practices that take advantage of consumers are prohibited

**Platform controls:**

| Control | Implementation |
|---------|---------------|
| Non-discriminatory models | Feature engineering review process; no proxy variables for protected classes. |
| Human override | All automated decisions for non-trivial cases require human review. Analysts can override any LLM/model decision. |
| Transparency | LLM generates plain-English explanations for every decision. |
| Accuracy | Evidence bundle grounding — LLM cannot invent facts not in the bundle. |
| Disparate impact monitoring | See ECOA section above. |

---

## 5. Gramm-Leach-Bliley Act (GLBA)

**Key requirements:**
- Safeguard nonpublic personal information (NPI)
- Privacy notices to consumers
- Data security program

**Platform controls:**

| Control | Implementation |
|---------|---------------|
| Encryption at rest | All S3 buckets, RDS, DynamoDB, Neptune use AWS KMS with key rotation. |
| Encryption in transit | All services use TLS 1.2+. MSK configured TLS-only. |
| SSN protection | SSNs hashed (SHA-256) immediately at intake; plain SSN never stored. |
| Access controls | IAM roles with least-privilege. VPC private subnets for all data services. |
| Audit logging | All data access logged to CloudWatch and PostgreSQL audit_trail. |

---

## 6. NIST AI Risk Management Framework (AI RMF)

The platform implements NIST AI RMF's four core functions:

### GOVERN
- Model registry with versioning (`model_registry` table)
- This compliance document and architecture docs
- Defined human review policy for all high-risk cases
- LLM output constraints (schema-bound, no free-form final decisions)

### MAP
- Risk taxonomy: 5 case types, 4 operational statuses, 6 fraud labels
- Rule engine maps deterministic evidence to coded findings
- Adverse action codes map findings to ECOA/FCRA categories

### MEASURE
- Model scores logged for every application (`model_scores` table)
- Drift monitoring via `ModelMonitor.compute_psi()`
- False positive rate tracked in `model_monitoring` table
- LLM confidence and latency logged in `llm_logs` table

### MANAGE
- Human review required for all non-trivial cases
- Analyst override mechanism with audit trail
- Model retraining pipeline triggered by drift alerts
- Incident response: fraud escalation → SAR → BSA officer

---

## 7. State Laws (Additional Considerations)

Some states have additional requirements beyond federal law:

| State | Additional Requirement |
|-------|----------------------|
| California (CCPA) | Consumer data deletion rights. Implement `DELETE /v1/persons/{id}` with audit trail. |
| New York (DFS 500) | Cybersecurity program; annual penetration testing. |
| Illinois (BIPA) | Biometric data (if device fingerprinting includes biometrics) requires consent. |
| Colorado (CCCPA) | Similar to CCPA. |

---

## Compliance Testing Checklist

### Before Production Launch
- [ ] ECOA adverse action notice delivery tested end-to-end
- [ ] Disparate impact analysis run on training data
- [ ] LLM output reviewed for demographic bias in 500 sample cases
- [ ] Audit trail immutability verified (attempt UPDATE/DELETE on audit_trail)
- [ ] SSN never appears in any log file (scan logs)
- [ ] All CloudWatch log groups confirm 7-year retention
- [ ] Penetration test completed
- [ ] Model documentation (Model Card) completed for all three models

### Ongoing (Monthly)
- [ ] Disparate impact analysis across protected classes
- [ ] PSI drift check for all three models
- [ ] False positive rate review
- [ ] LLM prompt injection audit
- [ ] SAR filing completeness review

### Ongoing (Quarterly)
- [ ] Full NIST AI RMF review
- [ ] Model performance validation vs. holdout data
- [ ] Rule engine review against current fraud patterns
- [ ] Access control audit
