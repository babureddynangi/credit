#!/usr/bin/env python3
"""
scripts/run_realistic_mvp.py
Realistic MVP pipeline -- runs a small batch through Ollama LLM classification
with scenario-aware graph data (no Docker, no PostgreSQL needed).

Uses:
  - Ollama llama3.2 for REAL LLM case classification
  - Scenario-aware mock graph data for realistic risk signals
  - All 12 deterministic rules
  - Full policy mapping + explanations

Usage:
    cd credit-fraud-platform
    python scripts/run_realistic_mvp.py
    python scripts/run_realistic_mvp.py --total 20 --batch-size 5
"""

import os, sys, json, random, hashlib, time, uuid, argparse, io
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from collections import Counter, defaultdict

# Force unbuffered output for real-time progress
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# ─── Force realistic backends ─────────────────────────────────────────────────
os.environ["MOCK_LLM"]       = "false"
os.environ["LLM_BACKEND"]    = "ollama"
os.environ["OLLAMA_BASE_URL"] = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
os.environ["OLLAMA_MODEL"]   = os.environ.get("OLLAMA_MODEL", "llama3.2")
os.environ["GRAPH_BACKEND"]  = "mock"   # keep graph mock (no Neo4j installed)
os.environ.setdefault("OPENAI_API_KEY", "mock-key")

from api.schemas.models import (
    ApplicationIntake, ModelScores, GraphRiskOutput, EvidenceBundle,
    RelatedParty, TimelineEvent, LLMCaseClassification,
    OperationalStatus, FraudLabel, CaseType, RuleSeverity,
)
from graph.analyzer import GraphAnalyzer
from rules.engine import RuleEngine
from llm.agents import IntakeAgent, LLMClassificationAgent, PolicyMappingAgent, LLMSummaryAgent

UTC = timezone.utc

# ── Config ────────────────────────────────────────────────────────────────────
RESULTS_DIR = ROOT / "results"
RESULTS_DIR.mkdir(exist_ok=True)

LOAN_PURPOSES = [
    "home_improvement","debt_consolidation","auto","medical",
    "education","business","personal","wedding","vacation",
]
FIRST_NAMES = [
    "James","Mary","John","Patricia","Robert","Jennifer","Michael","Linda",
    "William","Barbara","David","Susan","Richard","Jessica","Joseph","Sarah",
    "Thomas","Karen","Charles","Lisa","Christopher","Nancy","Daniel","Betty",
]
LAST_NAMES = [
    "Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis",
    "Rodriguez","Martinez","Hernandez","Lopez","Gonzalez","Wilson","Anderson",
    "Thomas","Taylor","Moore","Jackson","Martin","Lee","Perez","Thompson",
]
STATES = ["CA","TX","FL","NY","PA","IL","OH","GA","NC","MI"]

SCENARIOS = ["clean","related_party","proxy_borrower","fund_flow","coordinated_fraud","identity_misuse"]
WEIGHTS   = [50, 15, 12, 10, 8, 5]  # heavier fraud mix for smaller runs

rng = random.Random(42)

# ── Data generators ───────────────────────────────────────────────────────────

def rname():
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"

def rdob():
    return date.today() - timedelta(days=rng.randint(22*365, 70*365))

def bureau_score(scenario):
    if scenario == "clean":           return rng.randint(650, 820)
    if scenario in ("related_party","proxy_borrower"): return rng.randint(580, 720)
    return rng.randint(480, 680)

def loan_amount(scenario):
    if scenario == "coordinated_fraud": return round(rng.uniform(40_000, 200_000), 2)
    if scenario == "clean":             return round(rng.uniform(2_000,  50_000),  2)
    return round(rng.uniform(5_000, 80_000), 2)

def income(loan, scenario):
    if scenario in ("proxy_borrower","identity_misuse"):
        return round(loan * rng.uniform(0.8, 1.5), 2)
    return round(loan * rng.uniform(2.0, 8.0), 2)

def build_intake(i, scenario):
    loan = loan_amount(scenario)
    return ApplicationIntake(
        external_app_id=f"MVP-{i:05d}",
        external_person_id=f"PER-{i:05d}",
        full_name=rname(),
        dob=rdob(),
        ssn_last4=f"{rng.randint(1000,9999)}",
        loan_amount=loan,
        loan_purpose=rng.choice(LOAN_PURPOSES),
        declared_income=income(loan, scenario),
        address=f"{rng.randint(100,9999)} Main St, {rng.choice(STATES)}",
        phone=f"555-{rng.randint(1000,9999)}",
        email=f"user{i}@example.com",
        bank_account_hash=hashlib.sha256(f"acct-{i}".encode()).hexdigest()[:16],
        device_fingerprint=f"fp-{rng.randint(100000,999999)}",
        ip_address=f"10.{rng.randint(0,255)}.{rng.randint(0,255)}.{rng.randint(1,254)}",
        bureau_score=bureau_score(scenario),
        submitted_at=datetime.now(UTC) - timedelta(seconds=rng.randint(0, 86400)),
    )

def build_graph_risk(scenario, person_id):
    analyzer = GraphAnalyzer()
    if scenario == "clean":
        return GraphRiskOutput(
            related_parties=[], household_default_count=0,
            fund_flow_to_defaulter=False, cluster_density=0.0,
            graph_risk_score=round(rng.uniform(0.0, 0.12), 4),
        )
    default_days = rng.randint(10, 85)
    shared = rng.sample(["bank_account","device_fingerprint","address","phone","email"],
                        k=rng.randint(1, 3))
    party = RelatedParty(
        person_id=f"rel-{person_id}",
        name=rname(),
        relationship_type=rng.choice(["spouse","sibling","parent","business_partner"]),
        shared_attributes=shared,
        link_strength=round(rng.uniform(0.5, 1.0), 2),
        recent_default=(scenario in ("related_party","fund_flow","coordinated_fraud")),
        default_date=date.today() - timedelta(days=default_days),
        default_amount=round(rng.uniform(1000, 50000), 2),
    )
    hh_defaults = rng.randint(1, 5) if scenario != "clean" else 0
    fund_flow   = scenario in ("fund_flow","coordinated_fraud")
    density     = round(rng.uniform(0.3, 0.9), 4) if scenario == "coordinated_fraud" \
                  else round(rng.uniform(0.0, 0.4), 4)
    score = analyzer._compute_graph_risk_score(
        related_parties=[party],
        household_default_count=hh_defaults,
        shortest_path=rng.randint(1, 3),
        fund_flow_to_defaulter=fund_flow,
        cluster_density=density,
    )
    return GraphRiskOutput(
        related_parties=[party],
        household_default_count=hh_defaults,
        shortest_path_to_defaulter=rng.randint(1, 3),
        fund_flow_to_defaulter=fund_flow,
        cluster_density=density,
        graph_risk_score=round(score, 4),
    )

def heuristic_scores(intake, graph_risk):
    bureau = intake.bureau_score or 650
    pd_base    = max(0.02, min(0.95, (780 - bureau) / 400))
    fraud_base = min(1.0, 0.05 + graph_risk.graph_risk_score * 0.4)
    proxy_base = min(1.0, graph_risk.graph_risk_score * 0.7)
    return ModelScores(
        pd_score=round(pd_base, 4),
        fraud_score=round(fraud_base, 4),
        proxy_borrower_score=round(proxy_base, 4),
        model_version="heuristic-v0.1",
    )

def build_bundle(intake, graph_risk, scores, case_id, app_id, scenario):
    shared = list({a for p in graph_risk.related_parties for a in p.shared_attributes})
    fund_flow_signals = {
        "transferred_to_related_defaulter": graph_risk.fund_flow_to_defaulter,
        "transfer_hours_after_disbursement": rng.randint(1, 10)
            if graph_risk.fund_flow_to_defaulter else 999,
        "rapid_full_transfer": scenario == "coordinated_fraud",
        "purpose_mismatch": scenario in ("proxy_borrower","coordinated_fraud") and rng.random() < 0.4,
    }
    device_signals = {
        "device_fingerprint": intake.device_fingerprint,
        "device_in_fraud_registry": scenario == "identity_misuse" and rng.random() < 0.3,
        "phone_controlled_by_other_borrower": False,
        "recent_address_change_days": rng.randint(5, 25)
            if scenario == "identity_misuse" else 9999,
    }
    bureau_summary = {
        "score": intake.bureau_score,
        "declared_income": intake.declared_income,
        "bureau_income_estimate": intake.declared_income / rng.uniform(1.0, 3.5)
            if scenario in ("proxy_borrower","identity_misuse") else intake.declared_income,
    }
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
        bureau_summary=bureau_summary, device_signals=device_signals,
        fund_flow_signals=fund_flow_signals,
        policy_references=["ECOA","FCRA","BSA/AML","NIST-AI-RMF"],
    )

# ── Pipeline singletons ───────────────────────────────────────────────────────
rule_engine          = RuleEngine()
intake_agent         = IntakeAgent()
classification_agent = LLMClassificationAgent()
policy_agent         = PolicyMappingAgent()
summary_agent        = LLMSummaryAgent()

# ── Per-application pipeline ──────────────────────────────────────────────────

def run_one(i, scenario, timeout=60):
    case_id = str(uuid.uuid4())
    app_id  = str(uuid.uuid4())

    intake     = build_intake(i, scenario)
    graph_risk = build_graph_risk(scenario, f"per-{i}")
    scores     = heuristic_scores(intake, graph_risk)
    bundle     = build_bundle(intake, graph_risk, scores, case_id, app_id, scenario)

    # Rule engine
    rule_out = rule_engine.evaluate(bundle)
    bundle.rule_hits = rule_out.rule_hits

    # Intake validation
    validation = intake_agent.validate(bundle)

    # LLM classification — REAL Ollama call
    llm_start = time.time()
    try:
        classification, llm_log = classification_agent.classify(bundle)
        llm_time = time.time() - llm_start
        used_real_llm = llm_log.get("model", "") != "mock-llm"
    except Exception as e:
        llm_time = time.time() - llm_start
        used_real_llm = False
        # Fallback to mock
        classification = classification_agent._mock_classify(bundle)
        llm_log = classification_agent._mock_log(bundle)

    if rule_out.critical_hit:
        classification.human_review_required = True

    # Policy mapping
    policy_out = policy_agent.apply(classification, bundle)

    if policy_out["operational_status"] == OperationalStatus.FRAUD_ESCALATION \
            and policy_out["priority"] == 50:
        policy_out["priority"] = 10
        policy_out["sla_hours"] = 4
    elif policy_out["operational_status"] == OperationalStatus.MANUAL_REVIEW \
            and policy_out["priority"] == 50:
        policy_out["priority"] = 25
        policy_out["sla_hours"] = 12

    explanation = summary_agent.summarize(classification, bundle)

    return {
        "index":              i,
        "case_id":            case_id,
        "applicant_name":     intake.full_name,
        "scenario":           scenario,
        "loan_amount":        round(intake.loan_amount, 2),
        "bureau_score":       intake.bureau_score,
        "pd_score":           scores.pd_score,
        "fraud_score":        scores.fraud_score,
        "proxy_score":        scores.proxy_borrower_score,
        "graph_risk_score":   graph_risk.graph_risk_score,
        "rule_hits":          len(rule_out.rule_hits),
        "critical_hit":       rule_out.critical_hit,
        "rule_codes":         ", ".join(h.rule_code for h in rule_out.rule_hits),
        "llm_case_type":      classification.case_type.value,
        "llm_confidence":     classification.confidence,
        "llm_fraud_label":    classification.fraud_label.value,
        "llm_action":         classification.recommended_action.value,
        "llm_summary":        classification.analyst_summary[:200],
        "llm_latency_ms":     int(llm_time * 1000),
        "used_real_llm":      used_real_llm,
        "status":             policy_out["operational_status"].value,
        "priority":           policy_out["priority"],
        "sla_hours":          policy_out["sla_hours"],
        "explanation":        explanation[:200],
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Realistic MVP Pipeline Runner (Ollama LLM)")
    parser.add_argument("--total", type=int, default=10, help="Applications to process")
    parser.add_argument("--batch-size", type=int, default=5, help="Batch size")
    parser.add_argument("--batch-pause", type=float, default=1.0, help="Pause between batches (seconds)")
    args = parser.parse_args()

    TOTAL = args.total
    BATCH = args.batch_size
    PAUSE = args.batch_pause

    W = 72
    print(f"\n{'='*W}")
    print(f"  CREDIT FRAUD MVP -- REALISTIC PIPELINE (Ollama LLM)")
    print(f"{'='*W}")
    print(f"  LLM Backend   : Ollama ({os.environ['OLLAMA_MODEL']})")
    print(f"  Applications  : {TOTAL}")
    print(f"  Batch size    : {BATCH}")
    print(f"  Pipeline      : Graph -> ML -> Rules -> Intake -> LLM -> Policy -> Audit")
    print(f"{'='*W}\n")

    # Verify Ollama is reachable
    try:
        from openai import OpenAI
        client = OpenAI(api_key="ollama", base_url=os.environ["OLLAMA_BASE_URL"])
        models = client.models.list()
        print(f"  [OK] Ollama connected -- {len(models.data)} model(s) available")
    except Exception as e:
        print(f"  [FAIL] Ollama not reachable: {e}")
        print(f"    Start Ollama with: ollama serve")
        print(f"    Then pull model:   ollama pull {os.environ['OLLAMA_MODEL']}")
        return

    scenarios = rng.choices(SCENARIOS, weights=WEIGHTS, k=TOTAL)
    results = []
    status_counts = Counter()
    fraud_counts  = Counter()
    total_llm_ms  = 0
    real_llm_count = 0

    t0 = time.time()

    for batch_start in range(0, TOTAL, BATCH):
        batch_end = min(batch_start + BATCH, TOTAL)
        batch_num = batch_start // BATCH + 1
        total_batches = (TOTAL + BATCH - 1) // BATCH

        print(f"\n  --- Batch {batch_num}/{total_batches} (apps {batch_start+1}-{batch_end}) ---")

        for i in range(batch_start, batch_end):
            sc = scenarios[i]
            app_start = time.time()
            row = run_one(i + 1, sc)
            app_time = time.time() - app_start

            results.append(row)
            status_counts[row["status"]] += 1
            fraud_counts[row["llm_fraud_label"]] += 1
            total_llm_ms += row["llm_latency_ms"]
            if row["used_real_llm"]:
                real_llm_count += 1

            llm_tag = "[OLLAMA]" if row["used_real_llm"] else "[MOCK]"
            status_color = "[!]" if row["status"] == "fraud_escalation" else \
                          "[?]" if row["status"] == "manual_review" else "[+]"

            print(f"  {status_color} #{row['index']:>3}  {row['applicant_name']:<22} "
                  f"${row['loan_amount']:>10,.0f}  "
                  f"{row['scenario']:<20} -> {row['status']:<18} "
                  f"{llm_tag:>10} {row['llm_latency_ms']:>5}ms")

        if batch_start + BATCH < TOTAL:
            print(f"  ... pausing {PAUSE}s between batches ...")
            time.sleep(PAUSE)

    elapsed = time.time() - t0

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*W}")
    print(f"  RESULTS SUMMARY")
    print(f"{'='*W}")
    print(f"  Total processed    : {len(results)}")
    print(f"  Elapsed            : {elapsed:.1f}s")
    print(f"  Avg per app        : {elapsed/len(results)*1000:.0f}ms")
    print(f"  Real LLM calls     : {real_llm_count}/{len(results)} ({real_llm_count/len(results)*100:.0f}%)")
    print(f"  Avg LLM latency    : {total_llm_ms/max(real_llm_count,1):.0f}ms")

    print(f"\n  OPERATIONAL STATUS:")
    for st, cnt in sorted(status_counts.items(), key=lambda x: -x[1]):
        bar = "#" * int(cnt / len(results) * 30)
        print(f"    {st:<22} {cnt:>4}  ({cnt/len(results)*100:5.1f}%)  {bar}")

    print(f"\n  FRAUD LABELS (LLM):")
    for lb, cnt in sorted(fraud_counts.items(), key=lambda x: -x[1]):
        bar = "#" * int(cnt / len(results) * 30)
        print(f"    {lb:<35} {cnt:>4}  ({cnt/len(results)*100:5.1f}%)  {bar}")

    # Save results
    json_path = RESULTS_DIR / "realistic_mvp_results.json"
    summary = {
        "run_date":           datetime.now(UTC).isoformat(),
        "total_applications": len(results),
        "elapsed_seconds":    round(elapsed, 1),
        "llm_backend":        "ollama",
        "llm_model":          os.environ["OLLAMA_MODEL"],
        "real_llm_calls":     real_llm_count,
        "avg_llm_latency_ms": round(total_llm_ms / max(real_llm_count, 1)),
        "operational_status": dict(status_counts),
        "fraud_labels":       dict(fraud_counts),
        "applications":       results,
    }
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)

    print(f"\n  Results saved to: {json_path}")
    print(f"{'='*W}\n")


if __name__ == "__main__":
    main()
