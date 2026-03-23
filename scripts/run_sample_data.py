#!/usr/bin/env python3
"""
scripts/run_sample_data.py
Full-pipeline run of 100,000 synthetic loan applications.

Every stage fires:
  1. Graph analysis       (scenario-aware mock)
  2. Heuristic ML scoring
  3. Rule engine          (all 12 rules)
  4. Intake validation
  5. LLM classification   (scenario-aware mock — realistic labels)
  6. Policy mapping       (thresholds + rule overrides)
  7. Audit log            (in-memory, no DB needed)

Output (results/):
  sample_run_results.csv   — one row per application, all pipeline fields
  sample_run_summary.json  — aggregate stats
  sample_run_report.txt    — human-readable report

Usage:
    cd credit-fraud-platform
    python scripts/run_sample_data.py
"""

import os, sys, csv, json, random, hashlib, time, uuid
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from collections import Counter, defaultdict

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

os.environ["MOCK_LLM"]      = "true"
os.environ["GRAPH_BACKEND"] = "mock"
os.environ.setdefault("OPENAI_API_KEY", "mock-key")

from api.schemas.models import (
    ApplicationIntake, ModelScores, GraphRiskOutput, EvidenceBundle,
    RelatedParty, TimelineEvent, LLMCaseClassification,
    OperationalStatus, FraudLabel, CaseType, RuleSeverity,
)
from graph.analyzer import GraphAnalyzer
from rules.engine import RuleEngine
from llm.agents import IntakeAgent, PolicyMappingAgent, LLMSummaryAgent

UTC = timezone.utc

# ── Config ────────────────────────────────────────────────────────────────────
TOTAL      = 100_000
PRINT_EVERY = 10_000
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
    "Matthew","Margaret","Anthony","Sandra","Mark","Ashley","Donald","Dorothy",
]
LAST_NAMES = [
    "Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis",
    "Rodriguez","Martinez","Hernandez","Lopez","Gonzalez","Wilson","Anderson",
    "Thomas","Taylor","Moore","Jackson","Martin","Lee","Perez","Thompson",
]
STATES = ["CA","TX","FL","NY","PA","IL","OH","GA","NC","MI"]

# Scenario distribution: 85% clean, rest are fraud variants
SCENARIOS = ["clean","related_party","proxy_borrower","fund_flow","coordinated_fraud","identity_misuse"]
WEIGHTS   = [85, 5, 4, 3, 2, 1]

rng = random.Random(42)

# ── Scenario → LLM classification mapping ────────────────────────────────────
SCENARIO_LLM = {
    "clean": (
        CaseType.INDEPENDENT_CREDIT_RISK,
        FraudLabel.NONE,
        OperationalStatus.APPROVED,
        False,
        0.88,
        ["Standard credit evaluation — no fraud signals detected."],
        [],
    ),
    "related_party": (
        CaseType.RELATED_PARTY_RISK,
        FraudLabel.RELATED_PARTY_RISK,
        OperationalStatus.MANUAL_REVIEW,
        True,
        0.76,
        ["Related party recently defaulted within 90-day window.",
         "Shared attributes detected between applicant and defaulter."],
        ["RELATED_PARTY_DEFAULT"],
    ),
    "proxy_borrower": (
        CaseType.SUSPECTED_PROXY_BORROWER,
        FraudLabel.SUSPECTED_PROXY_BORROWER,
        OperationalStatus.MANUAL_REVIEW,
        True,
        0.72,
        ["Declared income inconsistent with bureau profile.",
         "Proxy borrower score elevated — possible third-party controller."],
        ["PROXY_BORROWER_RISK","1"],
    ),
    "fund_flow": (
        CaseType.RELATED_PARTY_RISK,
        FraudLabel.RELATED_PARTY_RISK,
        OperationalStatus.FRAUD_ESCALATION,
        True,
        0.81,
        ["Loan disbursement transferred to related defaulter within 24h.",
         "Fund flow anomaly confirmed by transaction analysis."],
        ["FUND_FLOW_ANOMALY","RELATED_PARTY_DEFAULT"],
    ),
    "coordinated_fraud": (
        CaseType.SUSPECTED_COORDINATED_FRAUD,
        FraudLabel.SUSPECTED_COORDINATED_FRAUD,
        OperationalStatus.FRAUD_ESCALATION,
        True,
        0.91,
        ["High-density related-party cluster detected.",
         "Multiple household defaults in 12-month window.",
         "Fund flow to known defaulter confirmed."],
        ["FUND_FLOW_ANOMALY","RELATED_PARTY_DEFAULT","PROXY_BORROWER_RISK"],
    ),
    "identity_misuse": (
        CaseType.SUSPECTED_IDENTITY_MISUSE,
        FraudLabel.SUSPECTED_IDENTITY_MISUSE,
        OperationalStatus.MANUAL_REVIEW,
        True,
        0.69,
        ["Device fingerprint shared with recent defaulter.",
         "Declared income significantly overstated vs bureau estimate."],
        ["PROXY_BORROWER_RISK","1"],
    ),
}

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
        return round(loan * rng.uniform(0.8, 1.5), 2)   # inflated
    return round(loan * rng.uniform(2.0, 8.0), 2)

def build_intake(i, scenario):
    loan = loan_amount(scenario)
    return ApplicationIntake(
        external_app_id=f"APP-{i:07d}",
        external_person_id=f"PER-{i:07d}",
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

def scenario_llm_classify(scenario, bundle):
    """Scenario-aware mock LLM — returns realistic classification per fraud type."""
    case_type, fraud_label, rec_action, human_review, confidence, reasons, aa_codes = \
        SCENARIO_LLM[scenario]
    summary = (
        f"Case {bundle.case_id} — {case_type.value.replace('_',' ').title()} "
        f"({confidence:.0%} confidence). "
        f"Key factors: {'; '.join(reasons[:2])}. "
        f"Action: {rec_action.value}. Human review: {human_review}."
    )
    return LLMCaseClassification(
        case_type=case_type, confidence=confidence,
        key_reasons=reasons, missing_evidence=[],
        recommended_action=rec_action,
        human_review_required=human_review,
        fraud_label=fraud_label,
        analyst_summary=summary,
        adverse_action_codes=aa_codes,
        next_investigation_steps=(
            ["Verify identity documents","Check related-party loan history"]
            if human_review else []
        ),
    )

# ── Pipeline singletons ───────────────────────────────────────────────────────
rule_engine   = RuleEngine()
intake_agent  = IntakeAgent()
policy_agent  = PolicyMappingAgent()
summary_agent = LLMSummaryAgent()

# ── Per-application pipeline ──────────────────────────────────────────────────

def run_one(i, scenario):
    case_id = str(uuid.uuid4())
    app_id  = str(uuid.uuid4())

    # Stage 1: intake
    intake = build_intake(i, scenario)

    # Stage 2: graph analysis
    graph_risk = build_graph_risk(scenario, f"per-{i}")

    # Stage 3: ML scoring
    scores = heuristic_scores(intake, graph_risk)

    # Stage 4: evidence bundle
    bundle = build_bundle(intake, graph_risk, scores, case_id, app_id, scenario)

    # Stage 5: rule engine (all 12 rules)
    rule_out = rule_engine.evaluate(bundle)
    bundle.rule_hits = rule_out.rule_hits

    # Stage 6: intake validation
    validation = intake_agent.validate(bundle)

    # Stage 7: LLM classification (scenario-aware mock)
    classification = scenario_llm_classify(scenario, bundle)
    # Enforce human review if rule engine says so
    if rule_out.critical_hit:
        classification.human_review_required = True

    # Stage 8: policy mapping
    policy_out = policy_agent.apply(classification, bundle)

    # Fix priority for LLM-driven escalations not caught by score thresholds
    if policy_out["operational_status"] == OperationalStatus.FRAUD_ESCALATION \
            and policy_out["priority"] == 50:
        policy_out["priority"] = 10
        policy_out["sla_hours"] = 4
    elif policy_out["operational_status"] == OperationalStatus.MANUAL_REVIEW \
            and policy_out["priority"] == 50:
        policy_out["priority"] = 25
        policy_out["sla_hours"] = 12

    # Stage 9: explanation
    explanation = summary_agent.summarize(classification, bundle)

    # Stage 10: in-memory audit record
    audit = {
        "audit_id":   str(uuid.uuid4()),
        "case_id":    case_id,
        "action":     "automated_decision",
        "actor_type": "llm",
        "status":     policy_out["operational_status"].value,
        "fraud_label": classification.fraud_label.value,
        "priority":   policy_out["priority"],
        "created_at": datetime.now(UTC).isoformat(),
    }

    # ── Build full output row ─────────────────────────────────────────────────
    rule_codes    = [h.rule_code for h in rule_out.rule_hits]
    rule_sevs     = [h.severity.value for h in rule_out.rule_hits]
    critical_codes = [h.rule_code for h in rule_out.rule_hits if h.severity == RuleSeverity.CRITICAL]
    warning_codes  = [h.rule_code for h in rule_out.rule_hits if h.severity == RuleSeverity.WARNING]

    related_names  = [p.name for p in graph_risk.related_parties]
    shared_attrs   = bundle.shared_attributes

    return {
        # ── Identity ──────────────────────────────────────────────────────────
        "index":                  i,
        "case_id":                case_id,
        "application_id":         app_id,
        "external_app_id":        intake.external_app_id,
        "external_person_id":     intake.external_person_id,
        "applicant_name":         intake.full_name,
        "dob":                    str(intake.dob),
        "loan_purpose":           intake.loan_purpose,
        "address_state":          intake.address.split(",")[-1].strip(),
        "submitted_at":           intake.submitted_at.isoformat(),

        # ── Financials ────────────────────────────────────────────────────────
        "loan_amount":            round(intake.loan_amount, 2),
        "declared_income":        round(intake.declared_income, 2),
        "bureau_score":           intake.bureau_score,
        "income_to_loan_ratio":   round(intake.declared_income / intake.loan_amount, 4),

        # ── ML Scores ─────────────────────────────────────────────────────────
        "pd_score":               scores.pd_score,
        "fraud_score":            scores.fraud_score,
        "proxy_borrower_score":   scores.proxy_borrower_score,
        "model_version":          scores.model_version,

        # ── Graph Analysis ────────────────────────────────────────────────────
        "graph_risk_score":       graph_risk.graph_risk_score,
        "household_default_count": graph_risk.household_default_count,
        "shortest_path_to_defaulter": graph_risk.shortest_path_to_defaulter,
        "fund_flow_to_defaulter": graph_risk.fund_flow_to_defaulter,
        "cluster_density":        graph_risk.cluster_density,
        "related_party_count":    len(graph_risk.related_parties),
        "related_party_names":    "|".join(related_names),
        "shared_attributes":      "|".join(shared_attrs),

        # ── Rule Engine ───────────────────────────────────────────────────────
        "rule_hit_count":         len(rule_out.rule_hits),
        "critical_hit":           rule_out.critical_hit,
        "manual_review_required_by_rules": rule_out.manual_review_required,
        "rule_codes_fired":       "|".join(rule_codes),
        "critical_rule_codes":    "|".join(critical_codes),
        "warning_rule_codes":     "|".join(warning_codes),

        # ── Intake Validation ─────────────────────────────────────────────────
        "intake_complete":        validation["complete"],
        "intake_missing_required": "|".join(validation["missing_required"]),
        "intake_missing_recommended": "|".join(validation["missing_recommended"]),

        # ── LLM Classification ────────────────────────────────────────────────
        "llm_case_type":          classification.case_type.value,
        "llm_confidence":         classification.confidence,
        "llm_fraud_label":        classification.fraud_label.value,
        "llm_recommended_action": classification.recommended_action.value,
        "llm_human_review":       classification.human_review_required,
        "llm_key_reasons":        " | ".join(classification.key_reasons),
        "llm_adverse_action_codes": "|".join(classification.adverse_action_codes),
        "llm_next_steps":         " | ".join(classification.next_investigation_steps),
        "llm_analyst_summary":    classification.analyst_summary,

        # ── Policy Output ─────────────────────────────────────────────────────
        "operational_status":     policy_out["operational_status"].value,
        "priority":               policy_out["priority"],
        "sla_hours":              policy_out["sla_hours"],
        "human_review_required":  policy_out["human_review_required"],
        "final_adverse_action_codes": "|".join(policy_out["adverse_action_codes"]),

        # ── Explanation ───────────────────────────────────────────────────────
        "explanation":            explanation,

        # ── Audit ─────────────────────────────────────────────────────────────
        "audit_id":               audit["audit_id"],
        "audit_created_at":       audit["created_at"],

        # ── Scenario (ground truth for evaluation) ────────────────────────────
        "scenario":               scenario,
    }

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\nCredit Fraud MVP — Full Pipeline Run: {TOTAL:,} applications")
    print("Stages: Graph > ML Scoring > Rule Engine > Intake Validation > LLM > Policy > Audit")
    print("=" * 72)

    csv_path  = RESULTS_DIR / "sample_run_results.csv"
    json_path = RESULTS_DIR / "sample_run_summary.json"
    txt_path  = RESULTS_DIR / "sample_run_report.txt"

    scenarios = rng.choices(SCENARIOS, weights=WEIGHTS, k=TOTAL)

    # Aggregation
    status_counts   = Counter()
    fraud_counts    = Counter()
    case_type_counts= Counter()
    scenario_stats  = defaultdict(lambda: {
        "total":0,"approved":0,"manual_review":0,"hold":0,
        "fraud_escalation":0,"declined":0,"rule_hits":0,"critical_hits":0,
    })
    total_loan = review_loan = escalated_loan = 0.0
    rule_hit_total = critical_total = 0
    priority_dist  = Counter()
    aa_code_counts = Counter()

    t0 = time.time()

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = None
        for i in range(1, TOTAL + 1):
            sc  = scenarios[i - 1]
            row = run_one(i, sc)

            if writer is None:
                writer = csv.DictWriter(f, fieldnames=list(row.keys()))
                writer.writeheader()
            writer.writerow(row)

            # Aggregate
            st = row["operational_status"]
            status_counts[st] += 1
            fraud_counts[row["llm_fraud_label"]] += 1
            case_type_counts[row["llm_case_type"]] += 1
            scenario_stats[sc]["total"] += 1
            scenario_stats[sc][st] = scenario_stats[sc].get(st, 0) + 1
            scenario_stats[sc]["rule_hits"] += row["rule_hit_count"]
            if row["critical_hit"]:
                scenario_stats[sc]["critical_hits"] += 1
                critical_total += 1
            total_loan  += row["loan_amount"]
            rule_hit_total += row["rule_hit_count"]
            priority_dist[row["priority"]] += 1
            if row["human_review_required"]:
                review_loan += row["loan_amount"]
            if st == "fraud_escalation":
                escalated_loan += row["loan_amount"]
            for code in row["final_adverse_action_codes"].split("|"):
                if code:
                    aa_code_counts[code] += 1

            if i % PRINT_EVERY == 0:
                elapsed = time.time() - t0
                rate = i / elapsed
                eta  = (TOTAL - i) / rate
                print(f"  {i:>7,} / {TOTAL:,}  |  {rate:,.0f} apps/sec  |  ETA {eta:.0f}s")

    elapsed = time.time() - t0

    # ── Summary JSON ──────────────────────────────────────────────────────────
    summary = {
        "run_date":            datetime.now(UTC).isoformat(),
        "total_applications":  TOTAL,
        "elapsed_seconds":     round(elapsed, 1),
        "throughput_per_sec":  round(TOTAL / elapsed, 1),
        "pipeline_stages":     ["graph_analysis","ml_scoring","rule_engine",
                                 "intake_validation","llm_classification",
                                 "policy_mapping","audit_log"],
        "total_loan_volume":   round(total_loan, 2),
        "review_loan_volume":  round(review_loan, 2),
        "escalated_loan_volume": round(escalated_loan, 2),
        "rule_hits_total":     rule_hit_total,
        "critical_hits_total": critical_total,
        "operational_status":  dict(status_counts),
        "fraud_label":         dict(fraud_counts),
        "case_type":           dict(case_type_counts),
        "priority_distribution": {str(k): v for k, v in sorted(priority_dist.items())},
        "adverse_action_codes": dict(aa_code_counts.most_common(10)),
        "scenario_breakdown":  {k: dict(v) for k, v in scenario_stats.items()},
    }
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)

    # ── Report ────────────────────────────────────────────────────────────────
    W = 72
    lines = [
        "=" * W,
        "  CREDIT FRAUD MVP — FULL PIPELINE RUN REPORT",
        "=" * W,
        f"  Pipeline stages  : Graph > ML > Rules > Intake > LLM > Policy > Audit",
        f"  Applications     : {TOTAL:>10,}",
        f"  Elapsed          : {elapsed:>10.1f}s",
        f"  Throughput       : {TOTAL/elapsed:>10,.0f} apps/sec",
        f"  Total loan vol   : ${total_loan:>14,.2f}",
        "",
        "  OPERATIONAL STATUS",
        "  " + "-" * 50,
    ]
    for st, cnt in sorted(status_counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {st:<28} {cnt:>8,}  ({cnt/TOTAL*100:5.1f}%)")

    lines += ["", "  FRAUD LABEL (LLM)", "  " + "-" * 50]
    for lb, cnt in sorted(fraud_counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {lb:<38} {cnt:>8,}  ({cnt/TOTAL*100:5.1f}%)")

    lines += ["", "  CASE TYPE (LLM)", "  " + "-" * 50]
    for ct, cnt in sorted(case_type_counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {ct:<38} {cnt:>8,}  ({cnt/TOTAL*100:5.1f}%)")

    lines += ["", "  SCENARIO BREAKDOWN", "  " + "-" * 50]
    for sc, st in sorted(scenario_stats.items(), key=lambda x: -x[1]["total"]):
        t = st["total"]
        esc = st.get("fraud_escalation", 0)
        rev = st.get("manual_review", 0)
        app = st.get("approved", 0)
        lines.append(
            f"  {sc:<22} n={t:>7,}  "
            f"approved={app/t*100:5.1f}%  "
            f"review={rev/t*100:5.1f}%  "
            f"escalated={esc/t*100:5.1f}%"
        )

    lines += ["", "  RULE ENGINE", "  " + "-" * 50]
    lines.append(f"  Total rule hits          : {rule_hit_total:>10,}")
    lines.append(f"  Critical hits            : {critical_total:>10,}")
    lines.append(f"  Avg hits per application : {rule_hit_total/TOTAL:>10.2f}")

    lines += ["", "  PRIORITY DISTRIBUTION", "  " + "-" * 50]
    for p, cnt in sorted(priority_dist.items()):
        label = {5:"FRAUD_ESC(2h)",10:"FRAUD_ESC(4h)",20:"HOLD(8h)",
                 25:"CRIT_RULE(12h)",30:"HIGH_PD(24h)",50:"STANDARD(48h)"}.get(p, str(p))
        lines.append(f"  Priority {p:<3} {label:<18} {cnt:>8,}  ({cnt/TOTAL*100:5.1f}%)")

    lines += ["", "  TOP ADVERSE ACTION CODES", "  " + "-" * 50]
    for code, cnt in aa_code_counts.most_common(8):
        lines.append(f"  {code:<35} {cnt:>8,}")

    lines += [
        "",
        "  LOAN EXPOSURE",
        "  " + "-" * 50,
        f"  Total portfolio          : ${total_loan:>14,.2f}",
        f"  Under manual review      : ${review_loan:>14,.2f}  ({review_loan/total_loan*100:.1f}%)",
        f"  Fraud escalated          : ${escalated_loan:>14,.2f}  ({escalated_loan/total_loan*100:.1f}%)",
        "",
        "  OUTPUT FILES",
        "  " + "-" * 50,
        f"  CSV  : {csv_path}",
        f"  JSON : {json_path}",
        f"  TXT  : {txt_path}",
        "=" * W,
    ]

    report = "\n".join(lines)
    print("\n" + report)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(f"\nDone. {TOTAL:,} rows written to {csv_path.name}")


if __name__ == "__main__":
    main()
