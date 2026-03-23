#!/usr/bin/env python3
"""
scripts/run_ollama_sim.py
Resource-controlled replacement for run_ollama_sample.py.
Adds per-call timeouts, concurrency limits, inter-batch pauses, and bounded retries.
"""

import os, sys, json, random, hashlib, time, uuid, argparse
from dataclasses import dataclass
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed, Future

import tenacity
import httpx
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from concurrent.futures import TimeoutError as FuturesTimeoutError

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

os.environ["MOCK_LLM"]        = "false"
os.environ["LLM_BACKEND"]     = "ollama"
os.environ["OLLAMA_BASE_URL"] = "http://localhost:11434/v1"
os.environ["OLLAMA_MODEL"]    = "llama3.2"
os.environ["GRAPH_BACKEND"]   = "mock"
os.environ.setdefault("OPENAI_API_KEY", "mock-key")

from api.schemas.models import (
    ApplicationIntake, ModelScores, GraphRiskOutput, EvidenceBundle,
    RelatedParty, TimelineEvent, OperationalStatus,
)
from graph.analyzer import GraphAnalyzer
from rules.engine import RuleEngine
from llm.agents import PolicyMappingAgent

UTC = timezone.utc
RESULTS_DIR = ROOT / "results"
RESULTS_DIR.mkdir(exist_ok=True)

LOAN_PURPOSES = ["home_improvement", "debt_consolidation", "auto", "medical", "business", "personal"]
FIRST_NAMES   = ["James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael", "Linda", "William", "Barbara"]
LAST_NAMES    = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez"]
STATES        = ["CA", "TX", "FL", "NY", "PA", "IL", "OH", "GA", "NC", "MI"]
SCENARIOS     = ["clean", "related_party", "proxy_borrower", "fund_flow", "coordinated_fraud", "identity_misuse"]
WEIGHTS       = [85, 5, 4, 3, 2, 1]

rng = random.Random(99)


# ─── Task 1: RunConfig & CLI ──────────────────────────────────────────────────

@dataclass
class RunConfig:
    total: int = 10
    batch_size: int = 5
    timeout: float = 30.0
    concurrency: int = 1
    batch_pause: float = 2.0
    retries: int = 2


def validate_config(config: "RunConfig") -> None:
    """Validate RunConfig; sys.exit(1) with descriptive message on failure."""
    if config.total < 1:
        print(f"Error: --total must be >= 1, got {config.total}", file=sys.stderr)
        sys.exit(1)
    if config.batch_size < 1:
        print(f"Error: --batch-size must be >= 1, got {config.batch_size}", file=sys.stderr)
        sys.exit(1)
    if config.batch_size > config.total:
        print(
            f"Error: --batch-size ({config.batch_size}) must not exceed --total ({config.total})",
            file=sys.stderr,
        )
        sys.exit(1)


def parse_args(argv=None) -> "RunConfig":
    parser = argparse.ArgumentParser(
        description="Resource-controlled Ollama simulation runner."
    )
    parser.add_argument("--total",       type=int,   default=10)
    parser.add_argument("--batch-size",  type=int,   default=5)
    parser.add_argument("--timeout",     type=float, default=30.0)
    parser.add_argument("--concurrency", type=int,   default=1)
    parser.add_argument("--batch-pause", type=float, default=2.0)
    parser.add_argument("--retries",     type=int,   default=2)
    args = parser.parse_args(argv)
    config = RunConfig(
        total=args.total,
        batch_size=args.batch_size,
        timeout=args.timeout,
        concurrency=args.concurrency,
        batch_pause=args.batch_pause,
        retries=args.retries,
    )
    validate_config(config)
    return config


# ─── Pipeline helpers (ported from run_ollama_sample.py) ─────────────────────

def _rname(): return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
def _rdob():  return date.today() - timedelta(days=rng.randint(22 * 365, 70 * 365))


def build_intake(i: int, scenario: str) -> ApplicationIntake:
    loan   = round(rng.uniform(5_000, 80_000), 2)
    bureau = rng.randint(580, 820) if scenario == "clean" else rng.randint(480, 720)
    income = round(loan * (rng.uniform(2.0, 6.0) if scenario == "clean" else rng.uniform(0.9, 2.0)), 2)
    return ApplicationIntake(
        external_app_id=f"SIM-{i:04d}",
        external_person_id=f"PER-{i:04d}",
        full_name=_rname(), dob=_rdob(),
        ssn_last4=f"{rng.randint(1000, 9999)}",
        loan_amount=loan, loan_purpose=rng.choice(LOAN_PURPOSES),
        declared_income=income,
        address=f"{rng.randint(100, 9999)} Main St, {rng.choice(STATES)}",
        phone=f"555-{rng.randint(1000, 9999)}",
        email=f"user{i}@example.com",
        bank_account_hash=hashlib.sha256(f"acct-{i}".encode()).hexdigest()[:16],
        device_fingerprint=f"fp-{rng.randint(100000, 999999)}",
        ip_address=f"10.{rng.randint(0,255)}.{rng.randint(0,255)}.{rng.randint(1,254)}",
        bureau_score=bureau,
        submitted_at=datetime.now(UTC) - timedelta(seconds=rng.randint(0, 86400)),
    )


def build_graph_risk(scenario: str, person_id: str) -> GraphRiskOutput:
    if scenario == "clean":
        return GraphRiskOutput(
            related_parties=[], household_default_count=0,
            fund_flow_to_defaulter=False, cluster_density=0.0,
            graph_risk_score=round(rng.uniform(0.0, 0.12), 4),
        )
    party = RelatedParty(
        person_id=f"rel-{person_id}", name=_rname(),
        relationship_type=rng.choice(["spouse", "sibling", "parent", "business_partner"]),
        shared_attributes=rng.sample(["bank_account", "device_fingerprint", "address"], k=rng.randint(1, 2)),
        link_strength=round(rng.uniform(0.5, 1.0), 2),
        recent_default=(scenario in ("related_party", "fund_flow", "coordinated_fraud")),
        default_date=date.today() - timedelta(days=rng.randint(10, 85)),
        default_amount=round(rng.uniform(1000, 50000), 2),
    )
    fund_flow = scenario in ("fund_flow", "coordinated_fraud")
    density   = round(rng.uniform(0.3, 0.9), 4) if scenario == "coordinated_fraud" else round(rng.uniform(0.0, 0.4), 4)
    analyzer  = GraphAnalyzer()
    score = analyzer._compute_graph_risk_score(
        related_parties=[party], household_default_count=rng.randint(1, 3),
        shortest_path=rng.randint(1, 3), fund_flow_to_defaulter=fund_flow, cluster_density=density,
    )
    return GraphRiskOutput(
        related_parties=[party], household_default_count=rng.randint(1, 3),
        shortest_path_to_defaulter=rng.randint(1, 3),
        fund_flow_to_defaulter=fund_flow, cluster_density=density,
        graph_risk_score=round(score, 4),
    )


def heuristic_scores(intake: ApplicationIntake, graph_risk: GraphRiskOutput) -> ModelScores:
    bureau = intake.bureau_score or 650
    return ModelScores(
        pd_score=round(max(0.02, min(0.95, (780 - bureau) / 400)), 4),
        fraud_score=round(min(1.0, 0.05 + graph_risk.graph_risk_score * 0.4), 4),
        proxy_borrower_score=round(min(1.0, graph_risk.graph_risk_score * 0.7), 4),
        model_version="heuristic-v0.1",
    )


def build_bundle(intake, graph_risk, scores, case_id, app_id, scenario) -> EvidenceBundle:
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
        policy_references=["ECOA", "FCRA", "BSA/AML", "NIST-AI-RMF"],
    )


def _unwrap(val):
    if not isinstance(val, dict):
        return val
    if "enum" in val and isinstance(val["enum"], list) and val["enum"]:
        return val["enum"][0]
    if val.get("type") in ("number", "integer") and "value" in val:
        return val["value"]
    t = val.get("type", "")
    if t not in ("string", "number", "boolean", "array", "object", "integer", "null", ""):
        return t
    return None


# ─── Task 2: ollama_classify_with_retry ──────────────────────────────────────

def ollama_classify_with_retry(bundle: EvidenceBundle, config: RunConfig):
    """
    Call Ollama llama3.2, parse and unwrap the response.
    Retries on json.JSONDecodeError, httpx.RequestError, openai.APIConnectionError.
    Returns (parsed_dict, latency_ms) or raises on exhausted retries.
    """
    import openai

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(config.retries + 1),
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
        retry=tenacity.retry_if_exception_type((json.JSONDecodeError, httpx.RequestError, openai.APIConnectionError)),
        reraise=True,
    )
    def _call():
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
        start = time.time()
        resp  = client.chat.completions.create(
            model="llama3.2",
            messages=[{"role": "user", "content": prompt}],
            temperature=0, max_tokens=600,
        )
        raw = resp.choices[0].message.content or ""
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw    = raw.strip()
        parsed = json.loads(raw)   # raises JSONDecodeError → retried
        for key in ["case_type", "recommended_action", "fraud_label"]:
            u = _unwrap(parsed.get(key))
            if u is not None:
                parsed[key] = u
        u = _unwrap(parsed.get("confidence"))
        if u is not None:
            parsed["confidence"] = u
        for key in ["key_reasons", "missing_evidence", "adverse_action_codes", "next_investigation_steps"]:
            if not isinstance(parsed.get(key), list):
                parsed[key] = []
        if not isinstance(parsed.get("human_review_required"), bool):
            parsed["human_review_required"] = bool(parsed.get("human_review_required", False))
        try:
            parsed["confidence"] = max(0.0, min(1.0, float(parsed.get("confidence", 0.7))))
        except Exception:
            parsed["confidence"] = 0.7
        if not isinstance(parsed.get("analyst_summary"), str):
            parsed["analyst_summary"] = str(parsed.get("analyst_summary", ""))
        latency_ms = int((time.time() - start) * 1000)
        return parsed, latency_ms

    return _call()


# ─── Task 3: run_one_with_timeout ─────────────────────────────────────────────

rule_engine  = RuleEngine()
policy_agent = PolicyMappingAgent()


def run_one_with_timeout(i: int, scenario: str, config: RunConfig) -> dict:
    """
    Run the full per-application pipeline with a hard timeout on the LLM call.
    Returns a result dict; error key is None on success, "timeout" or "llm_error" on failure.
    """
    case_id = str(uuid.uuid4())
    app_id  = str(uuid.uuid4())
    t0      = time.time()

    intake     = build_intake(i, scenario)
    graph_risk = build_graph_risk(scenario, f"per-{i}")
    scores     = heuristic_scores(intake, graph_risk)
    bundle     = build_bundle(intake, graph_risk, scores, case_id, app_id, scenario)
    rule_out   = rule_engine.evaluate(bundle)
    bundle.rule_hits = rule_out.rule_hits

    # Enforce timeout by running the LLM call in a separate thread
    with ThreadPoolExecutor(max_workers=1) as llm_executor:
        future: Future = llm_executor.submit(ollama_classify_with_retry, bundle, config)
        try:
            parsed, latency_ms = future.result(timeout=config.timeout)
        except (TimeoutError, FuturesTimeoutError):
            elapsed = time.time() - t0
            print(f"  [TIMEOUT] app={i} elapsed={elapsed:.1f}s", flush=True)
            return {
                "index": i, "scenario": scenario, "case_id": case_id,
                "loan_amount": round(intake.loan_amount, 2),
                "bureau_score": intake.bureau_score,
                "operational_status": "timeout",
                "llm_case_type": "", "llm_fraud_label": "", "llm_confidence": 0.0,
                "llm_human_review": False, "llm_key_reasons": "", "llm_analyst_summary": "",
                "rule_hits": len(rule_out.rule_hits), "critical_hit": rule_out.critical_hit,
                "latency_ms": int((time.time() - t0) * 1000),
                "error": "timeout",
            }
        except Exception:
            return {
                "index": i, "scenario": scenario, "case_id": case_id,
                "loan_amount": round(intake.loan_amount, 2),
                "bureau_score": intake.bureau_score,
                "operational_status": "llm_error",
                "llm_case_type": "", "llm_fraud_label": "", "llm_confidence": 0.0,
                "llm_human_review": False, "llm_key_reasons": "", "llm_analyst_summary": "",
                "rule_hits": len(rule_out.rule_hits), "critical_hit": rule_out.critical_hit,
                "latency_ms": int((time.time() - t0) * 1000),
                "error": "llm_error",
            }

    from api.schemas.models import LLMCaseClassification
    try:
        classification = LLMCaseClassification(**parsed)
    except Exception:
        return {
            "index": i, "scenario": scenario, "case_id": case_id,
            "loan_amount": round(intake.loan_amount, 2),
            "bureau_score": intake.bureau_score,
            "operational_status": "llm_error",
            "llm_case_type": "", "llm_fraud_label": "", "llm_confidence": 0.0,
            "llm_human_review": False, "llm_key_reasons": "", "llm_analyst_summary": "",
            "rule_hits": len(rule_out.rule_hits), "critical_hit": rule_out.critical_hit,
            "latency_ms": latency_ms,
            "error": "llm_error",
        }

    if rule_out.critical_hit:
        classification.human_review_required = True

    policy_out = policy_agent.apply(classification, bundle)

    return {
        "index": i, "scenario": scenario, "case_id": case_id,
        "loan_amount": round(intake.loan_amount, 2),
        "bureau_score": intake.bureau_score,
        "operational_status": policy_out["operational_status"].value,
        "llm_case_type": classification.case_type.value,
        "llm_fraud_label": classification.fraud_label.value,
        "llm_confidence": classification.confidence,
        "llm_human_review": classification.human_review_required,
        "llm_key_reasons": " | ".join(classification.key_reasons[:2]),
        "llm_analyst_summary": classification.analyst_summary[:200],
        "rule_hits": len(rule_out.rule_hits),
        "critical_hit": rule_out.critical_hit,
        "latency_ms": latency_ms,
        "error": None,
    }


# ─── Task 4: SimulationRunner batch loop ─────────────────────────────────────

def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def run_simulation(config: RunConfig, scenarios: list) -> list:
    """
    Main batch loop. Returns list of result dicts.
    Handles KeyboardInterrupt gracefully: drains in-flight futures and returns partial results.
    """
    rows      = []
    t0        = time.time()
    processed = 0
    errors    = 0
    indices   = list(range(1, config.total + 1))
    batches   = list(_chunks(indices, config.batch_size))

    try:
        with ThreadPoolExecutor(max_workers=config.concurrency) as executor:
            for batch_num, batch in enumerate(batches):
                futures = {
                    executor.submit(run_one_with_timeout, i, scenarios[i - 1], config): i
                    for i in batch
                }
                try:
                    for future in as_completed(futures):
                        result = future.result()
                        rows.append(result)
                        processed += 1
                        if result.get("error"):
                            errors += 1
                except KeyboardInterrupt:
                    print("\n  [INTERRUPT] Draining in-flight futures...", flush=True)
                    for f in futures:
                        if not f.done():
                            try:
                                result = f.result(timeout=config.timeout)
                                rows.append(result)
                                processed += 1
                                if result.get("error"):
                                    errors += 1
                            except Exception:
                                pass
                    raise

                elapsed    = time.time() - t0
                throughput = processed / elapsed if elapsed > 0 else 0.0
                print(
                    f"  Batch {batch_num + 1}/{len(batches)} | "
                    f"{processed}/{config.total} apps | "
                    f"{elapsed:.1f}s | {throughput:.2f} apps/sec | errors={errors}",
                    flush=True,
                )

                is_last = (batch_num == len(batches) - 1)
                if not is_last and config.batch_pause > 0:
                    time.sleep(config.batch_pause)

    except KeyboardInterrupt:
        print(f"\n  [INTERRUPT] Stopped after {processed} applications.", flush=True)

    return rows


# ─── Task 5: write_results ────────────────────────────────────────────────────

def write_results(rows: list, config: RunConfig, elapsed: float) -> None:
    """Write results/ollama_run_summary.json and results/ollama_run_report.txt."""
    from collections import Counter

    processed      = sum(1 for r in rows if not r.get("error"))
    timeout_errors = sum(1 for r in rows if r.get("error") == "timeout")
    llm_errors     = sum(1 for r in rows if r.get("error") == "llm_error")
    total_loan     = sum(r["loan_amount"] for r in rows)

    status_counts = Counter(r["operational_status"] for r in rows if not r.get("error"))
    fraud_counts  = Counter(r["llm_fraud_label"]    for r in rows if not r.get("error"))

    summary = {
        "run_date":          datetime.now(UTC).isoformat(),
        "llm_backend":       "ollama",
        "model":             "llama3.2",
        "total_applications": config.total,
        "processed":         processed,
        "timeout_errors":    timeout_errors,
        "llm_errors":        llm_errors,
        "elapsed_seconds":   round(elapsed, 1),
        "throughput_per_sec": round(len(rows) / elapsed, 2) if elapsed > 0 else 0.0,
        "total_loan_volume": round(total_loan, 2),
        "operational_status": dict(status_counts),
        "fraud_label":       dict(fraud_counts),
        "rows":              rows,
    }

    json_path = RESULTS_DIR / "ollama_run_summary.json"
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)

    W = 60
    safe_denom = len(rows) if len(rows) > 0 else 1
    report_lines = [
        "=" * W,
        "  OLLAMA (llama3.2) PIPELINE RUN REPORT",
        "=" * W,
        f"  Model            : llama3.2 (via Ollama)",
        f"  Applications     : {len(rows)}",
        f"  Processed OK     : {processed}",
        f"  Timeout errors   : {timeout_errors}",
        f"  LLM errors       : {llm_errors}",
        f"  Elapsed          : {elapsed:.1f}s",
        f"  Throughput       : {len(rows)/elapsed:.2f} apps/sec" if elapsed > 0 else "  Throughput       : N/A",
        f"  Total loan vol   : ${total_loan:,.2f}",
        "",
        "  OPERATIONAL STATUS",
        "  " + "-" * 40,
    ]
    for st, cnt in sorted(status_counts.items(), key=lambda x: -x[1]):
        report_lines.append(f"  {st:<28} {cnt:>5}  ({cnt/safe_denom*100:.1f}%)")
    report_lines += ["", "  FRAUD LABEL (Llama 3.2)", "  " + "-" * 40]
    for lb, cnt in sorted(fraud_counts.items(), key=lambda x: -x[1]):
        report_lines.append(f"  {lb:<35} {cnt:>5}  ({cnt/safe_denom*100:.1f}%)")
    report_lines += [
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


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    config    = parse_args()
    scenarios = rng.choices(SCENARIOS, weights=WEIGHTS, k=config.total)

    print(f"\nOllama (llama3.2) Simulation — {config.total} applications")
    print(f"  batch_size={config.batch_size}  timeout={config.timeout}s  "
          f"concurrency={config.concurrency}  batch_pause={config.batch_pause}s  "
          f"retries={config.retries}")
    print("=" * 60)

    t0   = time.time()
    rows = run_simulation(config, scenarios)
    write_results(rows, config, time.time() - t0)


if __name__ == "__main__":
    main()
