#!/usr/bin/env python3
"""
scripts/run_ollama_sample.py
Runs 100 synthetic loan applications through the REAL Llama 3.2 model via Ollama.
Produces results/ollama_run_summary.json and results/ollama_run_report.txt
"""

import os, sys, json, random, hashlib, time, uuid
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Use real Ollama LLM
os.environ["MOCK_LLM"]      = "false"
os.environ["LLM_BACKEND"]   = "ollama"
os.environ["OLLAMA_BASE_URL"] = "http://localhost:11434/v1"
os.environ["OLLAMA_MODEL"]  = "llama3.2"
os.environ["GRAPH_BACKEND"] = "mock"
os.environ.setdefault("OPENAI_API_KEY", "mock-key")

from api.schemas.models import (
    ApplicationIntake, ModelScores, GraphRiskOutput, EvidenceBundle,
    RelatedParty, TimelineEvent, OperationalStatus, FraudLabel, CaseType, RuleSeverity,
)
from graph.analyzer import GraphAnalyzer
from rules.engine import RuleEngine
from llm.agents import IntakeAgent, PolicyMappingAgent, LLMSummaryAgent

UTC = timezone.utc
TOTAL = 10
RESULTS_DIR = ROOT / "results"
RESULTS_DIR.mkdir(exist_ok=True)

LOAN_PURPOSES = ["home_improvement","debt_consolidation","auto","medical","business","personal"]
FIRST_NAMES = ["James","Mary","John","Patricia","Robert","Jennifer","Michael","Linda","William","Barbara"]
LAST_NAMES  = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Rodriguez","Martinez"]
STATES = ["CA","TX","FL","NY","PA","IL","OH","GA","NC","MI"]
SCENARIOS = ["clean","related_party","proxy_borrower","fund_flow","coordinated_fraud","identity_misuse"]
WEIGHTS   = [85, 5, 4, 3, 2, 1]

rng = random.Random(99)

def rname(): return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
def rdob():  return date.today() - timedelta(days=rng.randint(22*365, 70*365))

def build_intake(i, scenario):
    loan = round(rng.uniform(5_000, 80_000), 2)
    bureau = rng.randint(580, 820) if scenario == "clean" else rng.randint(480, 720)
    income = round(loan * (rng.uniform(2.0, 6.0) if scenario == "clean" else rng.uniform(0.9, 2.0)), 2)
    return ApplicationIntake(
        external_app_id=f"OLL-{i:04d}",
        external_person_id=f"PER-{i:04d}",
        full_name=rname(), dob=rdob(),
        ssn_last4=f"{rng.randint(1000,9999)}",
        loan_amount=loan, loan_purpose=rng.choice(LOAN_PURPOSES),
        declared_income=income,
        address=f"{rng.randint(100,9999)} Main St, {rng.choice(STATES)}",
        phone=f"555-{rng.randint(1000,9999)}",
        email=f"user{i}@example.com",
        bank_account_hash=hashlib.sha256(f"acct-{i}".encode()).hexdigest()[:16],
        device_fingerprint=f"fp-{rng.randint(100000,999999)}",
        ip_address=f"10.{rng.randint(0,255)}.{rng.randint(0,255)}.{rng.randint(1,254)}",
        bureau_score=bureau,
        submitted_at=datetime.now(UTC) - timedelta(seconds=rng.randint(0, 86400)),
    )

def build_graph_risk(scenario, person_id):
    if scenario == "clean":
        return GraphRiskOutput(
            related_parties=[], household_default_count=0,
            fund_flow_to_defaulter=False, cluster_density=0.0,
            graph_risk_score=round(rng.uniform(0.0, 0.12), 4),
        )
    party = RelatedParty(
        person_id=f"rel-{person_id}", name=rname(),
        relationship_type=rng.choice(["spouse","sibling","parent","business_partner"]),
        shared_attributes=rng.sample(["bank_account","device_fingerprint","address"], k=rng.randint(1,2)),
        link_strength=round(rng.uniform(0.5, 1.0), 2),
        recent_default=(scenario in ("related_party","fund_flow","coordinated_fraud")),
        default_date=date.today() - timedelta(days=rng.randint(10, 85)),
        default_amount=round(rng.uniform(1000, 50000), 2),
    )
    fund_flow = scenario in ("fund_flow","coordinated_fraud")
    density   = round(rng.uniform(0.3, 0.9), 4) if scenario == "coordinated_fraud" else round(rng.uniform(0.0, 0.4), 4)
    analyzer  = GraphAnalyzer()
    score = analyzer._compute_graph_risk_score(
        related_parties=[party], household_default_count=rng.randint(1,3),
        shortest_path=rng.randint(1,3), fund_flow_to_defaulter=fund_flow, cluster_density=density,
    )
    return GraphRiskOutput(
        related_parties=[party], household_default_count=rng.randint(1,3),
        shortest_path_to_defaulter=rng.randint(1,3),
        fund_flow_to_defaulter=fund_flow, cluster_density=density,
        graph_risk_score=round(score, 4),
    )

def build_bundle(intake, graph_risk, scores, case_id, app_id, scenario):
    shared = list({a for p in graph_risk.related_parties for a in p.shared_attributes})
    timeline = [TimelineEvent(
        timestamp=intake.submitted_at,
        event=f"Application submitted for ${intake.loan_amount:,.0f} ({intake.loan_purpose})",
        source="LOS",
    )]
    for p in graph_risk.related_parties:
        if p.recent_default and p.default_date:
            timeline.append(TimelineEvent(
                timestamp=datetime.combine(p.default_date, datetime.min.time()).replace(tzinfo=UTC),
                event=f"Related party '{p.name}' defaulted on ${p.default_amount or 0:,.0f}",
                source="collections_system",
            ))
    timeline.sort(key=lambda e: e.timestamp)
    return EvidenceBundle(
        case_id=case_id, application_id=app_id,
        applicant_name=intake.full_name, loan_amount=intake.loan_amount,
        submitted_at=intake.submitted_at, timeline=timeline,
        related_parties=graph_risk.related_parties, shared_attributes=shared,
        scores=scores, rule_hits=[], graph_risk=graph_risk,
        bureau_summary={"score": intake.bureau_score, "declared_income": intake.declared_income},
        device_signals={"device_fingerprint": intake.device_fingerprint, "device_in_fraud_registry": False},
        fund_flow_signals={"transferred_to_related_defaulter": graph_risk.fund_flow_to_defaulter},
        policy_references=["ECOA","FCRA","BSA/AML","NIST-AI-RMF"],
    )

def _unwrap(val):
    """Unwrap JSON-Schema-wrapped values that Llama sometimes returns."""
    if not isinstance(val, dict):
        return val
    if "enum" in val and isinstance(val["enum"], list) and val["enum"]:
        return val["enum"][0]
    if val.get("type") in ("number", "integer") and "value" in val:
        return val["value"]
    t = val.get("type", "")
    if t not in ("string","number","boolean","array","object","integer","null",""):
        return t
    return None

def ollama_classify(bundle):
    """Call Ollama llama3.2 directly, parse and unwrap the response."""
    from openai import OpenAI
    client = OpenAI(api_key="ollama", base_url="http://localhost:11434/v1")
    prompt = f"""Analyze this loan application evidence and respond with JSON only.

Applicant: {bundle.applicant_name}
Loan: ${bundle.loan_amount:,.0f}
Bureau score: {bundle.scores.pd_score:.0%} default risk, {bundle.scores.fraud_score:.0%} fraud risk
Graph risk: {bundle.graph_risk.graph_risk_score:.0%}, household defaults: {bundle.graph_risk.household_default_count}
Fund flow to defaulter: {bundle.graph_risk.fund_flow_to_defaulter}
Related parties: {len(bundle.related_parties)}
Rule hits: {len(bundle.rule_hits)} ({sum(1 for h in bundle.rule_hits if h.severity.value == 'critical')} critical)

Respond with this exact JSON (no markdown, no extra text):
{{
  "case_type": "<one of: independent_credit_risk|related_party_risk|suspected_proxy_borrower|suspected_identity_misuse|suspected_coordinated_fraud>",
  "confidence": <float 0-1>,
  "key_reasons": ["<reason1>", "<reason2>"],
  "missing_evidence": [],
  "recommended_action": "<one of: approved|manual_review|hold|fraud_escalation|declined>",
  "human_review_required": <true|false>,
  "fraud_label": "<one of: none|related_party_risk|suspected_proxy_borrower|suspected_identity_misuse|suspected_coordinated_fraud|confirmed_fraud>",
  "analyst_summary": "<plain English summary>",
  "adverse_action_codes": [],
  "next_investigation_steps": []
}}"""
    import time, json, hashlib
    start = time.time()
    try:
        resp = client.chat.completions.create(
            model="llama3.2",
            messages=[{"role": "user", "content": prompt}],
            temperature=0, max_tokens=600,
        )
        raw = resp.choices[0].message.content or ""
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        raw = raw.strip()
        parsed = json.loads(raw)
        # Unwrap any schema-wrapped values
        for key in ["case_type", "recommended_action", "fraud_label"]:
            u = _unwrap(parsed.get(key))
            if u is not None: parsed[key] = u
        u = _unwrap(parsed.get("confidence"))
        if u is not None: parsed["confidence"] = u
        for key in ["key_reasons","missing_evidence","adverse_action_codes","next_investigation_steps"]:
            if not isinstance(parsed.get(key), list): parsed[key] = []
        if not isinstance(parsed.get("human_review_required"), bool):
            parsed["human_review_required"] = bool(parsed.get("human_review_required", False))
        try: parsed["confidence"] = max(0.0, min(1.0, float(parsed.get("confidence", 0.7))))
        except: parsed["confidence"] = 0.7
        if not isinstance(parsed.get("analyst_summary"), str):
            parsed["analyst_summary"] = str(parsed.get("analyst_summary",""))
        latency_ms = int((time.time() - start) * 1000)
        return parsed, latency_ms, None
    except Exception as e:
        return None, int((time.time() - start) * 1000), str(e)

rule_engine   = RuleEngine()
intake_agent  = IntakeAgent()
policy_agent  = PolicyMappingAgent()
summary_agent = LLMSummaryAgent()

def main():
    print(f"\nOllama (llama3.2) Pipeline Run — {TOTAL} applications")
    print("=" * 60)

    scenarios = rng.choices(SCENARIOS, weights=WEIGHTS, k=TOTAL)
    status_counts = Counter()
    fraud_counts  = Counter()
    case_type_counts = Counter()
    total_loan = review_loan = escalated_loan = 0.0
    rule_hit_total = critical_total = 0
    llm_errors = 0
    rows = []

    t0 = time.time()
    for i in range(1, TOTAL + 1):
        sc = scenarios[i - 1]
        case_id = str(uuid.uuid4())
        app_id  = str(uuid.uuid4())

        intake     = build_intake(i, sc)
        graph_risk = build_graph_risk(sc, f"per-{i}")
        bureau     = intake.bureau_score or 650
        scores     = ModelScores(
            pd_score=round(max(0.02, min(0.95, (780 - bureau) / 400)), 4),
            fraud_score=round(min(1.0, 0.05 + graph_risk.graph_risk_score * 0.4), 4),
            proxy_borrower_score=round(min(1.0, graph_risk.graph_risk_score * 0.7), 4),
            model_version="heuristic-v0.1",
        )
        bundle = build_bundle(intake, graph_risk, scores, case_id, app_id, sc)
        rule_out = rule_engine.evaluate(bundle)
        bundle.rule_hits = rule_out.rule_hits

        try:
            parsed, latency_ms, err = ollama_classify(bundle)
            if parsed is None:
                print(f"  [WARN] Case {i} LLM error: {err}")
                llm_errors += 1
                continue
            used_fallback = False
            # Build classification from parsed dict
            from api.schemas.models import CaseType, FraudLabel
            classification = LLMCaseClassification(
                case_type=parsed["case_type"],
                confidence=parsed["confidence"],
                key_reasons=parsed["key_reasons"],
                missing_evidence=parsed["missing_evidence"],
                recommended_action=parsed["recommended_action"],
                human_review_required=parsed["human_review_required"],
                fraud_label=parsed["fraud_label"],
                analyst_summary=parsed["analyst_summary"],
                adverse_action_codes=parsed["adverse_action_codes"],
                next_investigation_steps=parsed["next_investigation_steps"],
            )
        except Exception as e:
            print(f"  [WARN] Case {i} parse error: {e}")
            llm_errors += 1
            continue

        if rule_out.critical_hit:
            classification.human_review_required = True

        policy_out = policy_agent.apply(classification, bundle)
        if policy_out["operational_status"] == OperationalStatus.FRAUD_ESCALATION and policy_out["priority"] == 50:
            policy_out["priority"] = 10; policy_out["sla_hours"] = 4
        elif policy_out["operational_status"] == OperationalStatus.MANUAL_REVIEW and policy_out["priority"] == 50:
            policy_out["priority"] = 25; policy_out["sla_hours"] = 12

        st = policy_out["operational_status"].value
        status_counts[st] += 1
        fraud_counts[classification.fraud_label.value] += 1
        case_type_counts[classification.case_type.value] += 1
        total_loan += intake.loan_amount
        rule_hit_total += len(rule_out.rule_hits)
        if rule_out.critical_hit: critical_total += 1
        if policy_out["human_review_required"]: review_loan += intake.loan_amount
        if st == "fraud_escalation": escalated_loan += intake.loan_amount

        rows.append({
            "index": i, "scenario": sc, "case_id": case_id,
            "loan_amount": round(intake.loan_amount, 2),
            "bureau_score": intake.bureau_score,
            "operational_status": st,
            "llm_case_type": classification.case_type.value,
            "llm_fraud_label": classification.fraud_label.value,
            "llm_confidence": classification.confidence,
            "llm_human_review": classification.human_review_required,
            "llm_key_reasons": " | ".join(classification.key_reasons[:2]),
            "llm_analyst_summary": classification.analyst_summary[:200],
            "rule_hits": len(rule_out.rule_hits),
            "critical_hit": rule_out.critical_hit,
            "used_fallback": used_fallback if 'used_fallback' in dir() else False,
        })

        if i % 10 == 0:
            elapsed = time.time() - t0
            print(f"  {i:>4}/{TOTAL}  |  {i/elapsed:.1f} apps/sec  |  errors={llm_errors}")

    elapsed = time.time() - t0
    processed = len(rows)

    summary = {
        "run_date": datetime.now(UTC).isoformat(),
        "llm_backend": "ollama",
        "model": "llama3.2",
        "total_applications": TOTAL,
        "processed": processed,
        "llm_errors_or_fallbacks": llm_errors,
        "elapsed_seconds": round(elapsed, 1),
        "throughput_per_sec": round(processed / elapsed, 2),
        "total_loan_volume": round(total_loan, 2),
        "review_loan_volume": round(review_loan, 2),
        "escalated_loan_volume": round(escalated_loan, 2),
        "rule_hits_total": rule_hit_total,
        "critical_hits_total": critical_total,
        "operational_status": dict(status_counts),
        "fraud_label": dict(fraud_counts),
        "case_type": dict(case_type_counts),
        "sample_cases": rows[:5],
    }

    json_path = RESULTS_DIR / "ollama_run_summary.json"
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)

    W = 60
    report_lines = [
        "=" * W,
        "  OLLAMA (llama3.2) PIPELINE RUN REPORT",
        "=" * W,
        f"  Model            : llama3.2 (via Ollama)",
        f"  Applications     : {processed}",
        f"  LLM errors/fallbacks: {llm_errors}",
        f"  Elapsed          : {elapsed:.1f}s",
        f"  Throughput       : {processed/elapsed:.2f} apps/sec",
        f"  Total loan vol   : ${total_loan:,.2f}",
        "",
        "  OPERATIONAL STATUS",
        "  " + "-" * 40,
    ]
    for st, cnt in sorted(status_counts.items(), key=lambda x: -x[1]):
        report_lines.append(f"  {st:<28} {cnt:>5}  ({cnt/processed*100:.1f}%)")
    report_lines += ["", "  FRAUD LABEL (Llama 3.2)", "  " + "-" * 40]
    for lb, cnt in sorted(fraud_counts.items(), key=lambda x: -x[1]):
        report_lines.append(f"  {lb:<35} {cnt:>5}  ({cnt/processed*100:.1f}%)")
    report_lines += ["", "  CASE TYPE (Llama 3.2)", "  " + "-" * 40]
    for ct, cnt in sorted(case_type_counts.items(), key=lambda x: -x[1]):
        report_lines.append(f"  {ct:<35} {cnt:>5}  ({cnt/processed*100:.1f}%)")
    report_lines += [
        "",
        "  LOAN EXPOSURE",
        "  " + "-" * 40,
        f"  Total portfolio  : ${total_loan:>14,.2f}",
        f"  Under review     : ${review_loan:>14,.2f}  ({review_loan/total_loan*100:.1f}% if total_loan else 0)",
        f"  Fraud escalated  : ${escalated_loan:>14,.2f}",
        "",
        f"  JSON: {json_path}",
        "=" * W,
    ]
    report = "\n".join(report_lines)
    print("\n" + report)

    txt_path = RESULTS_DIR / "ollama_run_report.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(f"\nDone. Summary: {json_path}")

if __name__ == "__main__":
    main()
