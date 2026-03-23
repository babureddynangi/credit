# api/main.py
# FastAPI Decision Orchestration API
# Orchestrates: ETL → Entity Resolution → Rules → Graph → ML → LLM → Policy → Audit

from __future__ import annotations
import os
import uuid
import logging
import hashlib
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.schemas.models import (
    ApplicationIntake, DecisionOutput, AnalystDecision,
    EvidenceBundle, ModelScores, GraphRiskOutput, TimelineEvent,
    OperationalStatus, FraudLabel, LLMCaseClassification,
    RelatedParty, RuleHit,
)
from rules.engine import RuleEngine
from graph.analyzer import GraphAnalyzer
from llm.agents import (
    IntakeAgent, LLMClassificationAgent, PolicyMappingAgent, LLMSummaryAgent
)
from governance.audit import AuditLogger
from governance.queue import publish_case_event

logger = logging.getLogger(__name__)
UTC = timezone.utc

# ─── App Setup ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Credit Fraud Platform API",
    description="US-compliant credit labeling and fraud risk detection API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)

# ─── Database Pool ────────────────────────────────────────────────────────────

_db_pool: Optional[asyncpg.Pool] = None

async def get_db() -> asyncpg.Connection:
    global _db_pool
    if _db_pool is None:
        _db_pool = await asyncpg.create_pool(
            dsn=os.environ["DATABASE_URL"],
            min_size=5,
            max_size=20,
            command_timeout=30,
        )
    async with _db_pool.acquire() as conn:
        yield conn

# ─── Service Singletons ───────────────────────────────────────────────────────

rule_engine         = RuleEngine()
graph_analyzer      = GraphAnalyzer()
intake_agent        = IntakeAgent()
classification_agent = LLMClassificationAgent()
policy_agent        = PolicyMappingAgent()
summary_agent       = LLMSummaryAgent()
audit_logger        = AuditLogger()


# ─── Middleware: Request ID + Audit ───────────────────────────────────────────

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(UTC).isoformat()}


# ─── Core Decision Endpoint ───────────────────────────────────────────────────

@app.post("/v1/applications/evaluate", response_model=DecisionOutput)
async def evaluate_application(
    intake: ApplicationIntake,
    background_tasks: BackgroundTasks,
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Full end-to-end evaluation of a loan application.

    Flow:
    1. Validate + store application
    2. Entity resolution (person_id, household_id)
    3. Graph analysis (related-party risk)
    4. Rule engine (deterministic policy checks)
    5. ML scoring (PD, fraud, proxy borrower)
    6. Build evidence bundle
    7. LLM classification + explanation
    8. Policy engine (final status + adverse action codes)
    9. Store case + audit trail
    10. Return DecisionOutput
    """
    request_start = datetime.now(UTC)
    case_id = str(uuid.uuid4())
    application_id = str(uuid.uuid4())

    try:
        # ── Step 1: Store application ────────────────────────────────────────
        person_id = await _upsert_person(db, intake)
        await _store_application(db, application_id, person_id, intake)

        # ── Step 2: Graph analysis ────────────────────────────────────────────
        graph_risk = graph_analyzer.analyze(person_id, application_id)

        # ── Step 3: ML Scores ─────────────────────────────────────────────────
        # In production: calls Ray Serve endpoints
        scores = await _get_ml_scores(intake, graph_risk, person_id)

        # ── Step 4: Build evidence bundle ─────────────────────────────────────
        bundle = _build_evidence_bundle(
            case_id, application_id, intake, graph_risk, scores, person_id
        )

        # ── Step 5: Rule engine ───────────────────────────────────────────────
        rule_output = rule_engine.evaluate(bundle)
        bundle.rule_hits = rule_output.rule_hits

        # ── Step 6: Intake agent validation ──────────────────────────────────
        validation = intake_agent.validate(bundle)

        # ── Step 7: LLM classification ────────────────────────────────────────
        classification, llm_log = classification_agent.classify(bundle)

        # ── Step 8: Policy engine ─────────────────────────────────────────────
        policy_output = policy_agent.apply(classification, bundle)
        explanation   = summary_agent.summarize(classification, bundle)

        # ── Step 9: Store case ────────────────────────────────────────────────
        await _store_case(
            db, case_id, application_id, classification,
            bundle, policy_output
        )

        # ── Step 10: Audit (background) ───────────────────────────────────────
        background_tasks.add_task(
            audit_logger.log_decision,
            case_id=case_id,
            application_id=application_id,
            bundle=bundle,
            classification=classification,
            policy_output=policy_output,
            llm_log=llm_log,
            latency_ms=int((datetime.now(UTC) - request_start).total_seconds() * 1000),
        )

        # ── Step 11: SQS notification (background) ────────────────────────────
        background_tasks.add_task(
            publish_case_event,
            case_id=case_id,
            status=str(policy_output["operational_status"].value),
        )

        return DecisionOutput(
            application_id=application_id,
            case_id=case_id,
            operational_status=policy_output["operational_status"],
            fraud_label=classification.fraud_label,
            llm_classification=classification,
            rule_hits=rule_output.rule_hits,
            scores=scores,
            adverse_action_codes=policy_output["adverse_action_codes"],
            human_review_required=policy_output["human_review_required"],
            sla_hours=policy_output["sla_hours"],
            decided_at=datetime.now(UTC),
            model_version=scores.model_version,
            explanation=explanation,
        )

    except Exception as e:
        logger.exception(f"Evaluation failed for case {case_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Evaluation failed: {str(e)}")


# ─── Analyst Decision Endpoint ────────────────────────────────────────────────

@app.post("/v1/cases/{case_id}/decision")
async def analyst_decision(
    case_id: str,
    decision: AnalystDecision,
    background_tasks: BackgroundTasks,
    db: asyncpg.Connection = Depends(get_db),
):
    """Record a human analyst's final disposition on a case."""
    case = await db.fetchrow(
        "SELECT * FROM cases WHERE case_id = $1", case_id
    )
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    if case["status"] in ("closed_approved", "closed_declined", "closed_fraud"):
        raise HTTPException(status_code=409, detail="Case already closed")

    status_map = {
        "approved":        "closed_approved",
        "declined":        "closed_declined",
        "escalated":       "escalated",
        "fraud_confirmed": "closed_fraud",
    }

    new_status = status_map[decision.disposition]

    await db.execute("""
        UPDATE cases
        SET status = $1, final_disposition = $2,
            disposition_reason = $3, closed_at = NOW(),
            updated_at = NOW()
        WHERE case_id = $4
    """, new_status, decision.disposition,
        "; ".join(decision.reason_codes), case_id)

    background_tasks.add_task(
        audit_logger.log_analyst_decision,
        case_id=case_id,
        analyst_id=decision.analyst_id,
        disposition=decision.disposition,
        reason_codes=decision.reason_codes,
        notes=decision.notes,
        override_llm=decision.override_llm,
    )

    return {"case_id": case_id, "status": new_status, "recorded_at": datetime.now(UTC).isoformat()}


# ─── Case Retrieval ───────────────────────────────────────────────────────────

@app.get("/v1/cases/{case_id}")
async def get_case(case_id: str, db: asyncpg.Connection = Depends(get_db)):
    """Retrieve full case with evidence bundle for analyst workbench."""
    row = await db.fetchrow("""
        SELECT c.*, a.loan_amount, a.fraud_label, a.operational_status,
               a.pd_score, a.fraud_score, a.proxy_borrower_score,
               p.full_name, p.household_id
        FROM cases c
        JOIN applications a ON a.application_id = c.application_id
        JOIN persons p ON p.person_id = a.person_id
        WHERE c.case_id = $1
    """, case_id)

    if not row:
        raise HTTPException(status_code=404, detail="Case not found")

    rule_hits = await db.fetch(
        "SELECT * FROM rule_hits WHERE case_id = $1 ORDER BY fired_at", case_id
    )

    return {
        "case": dict(row),
        "rule_hits": [dict(r) for r in rule_hits],
    }


@app.get("/v1/cases")
async def list_cases(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: asyncpg.Connection = Depends(get_db),
):
    """List cases for analyst queue."""
    query = """
        SELECT c.case_id, c.case_type, c.priority, c.status,
               c.sla_deadline, c.created_at,
               a.loan_amount, p.full_name
        FROM cases c
        JOIN applications a ON a.application_id = c.application_id
        JOIN persons p ON p.person_id = a.person_id
        {where}
        ORDER BY c.priority ASC, c.created_at ASC
        LIMIT $1 OFFSET $2
    """
    if status:
        rows = await db.fetch(
            query.format(where="WHERE c.status = $3"),
            limit, offset, status
        )
    else:
        rows = await db.fetch(
            query.format(where="WHERE c.status NOT IN ('closed_approved','closed_declined','closed_fraud')"),
            limit, offset
        )
    return {"cases": [dict(r) for r in rows], "total": len(rows)}


# ─── Helper Functions ─────────────────────────────────────────────────────────

async def _upsert_person(db: asyncpg.Connection, intake: ApplicationIntake) -> str:
    ssn_hash = hashlib.sha256(intake.ssn_last4.encode()).hexdigest()
    existing = await db.fetchrow(
        "SELECT person_id FROM persons WHERE external_id = $1", intake.external_person_id
    )
    if existing:
        return str(existing["person_id"])

    person_id = str(uuid.uuid4())
    await db.execute("""
        INSERT INTO persons (person_id, external_id, full_name, ssn_hash, dob)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (external_id) DO UPDATE SET full_name = $3, updated_at = NOW()
    """, person_id, intake.external_person_id, intake.full_name, ssn_hash, intake.dob)
    return person_id


async def _store_application(
    db: asyncpg.Connection, app_id: str, person_id: str, intake: ApplicationIntake
):
    await db.execute("""
        INSERT INTO applications
            (application_id, person_id, external_app_id, loan_amount,
             loan_purpose, declared_income, submitted_at, bureau_score)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        ON CONFLICT (external_app_id) DO NOTHING
    """, app_id, person_id, intake.external_app_id, intake.loan_amount,
        intake.loan_purpose, intake.declared_income,
        intake.submitted_at, intake.bureau_score)


async def _get_ml_scores(
    intake: ApplicationIntake,
    graph_risk: GraphRiskOutput,
    person_id: str,
) -> ModelScores:
    """
    In production: calls Ray Serve model endpoints.
    Dev fallback: uses heuristic scoring.
    """
    ray_serve_url = os.getenv("RAY_SERVE_URL", "")

    if ray_serve_url:
        import httpx
        evidence = {
            "bureau_score":     intake.bureau_score,
            "loan_amount":      intake.loan_amount,
            "declared_income":  intake.declared_income,
            "graph":            graph_risk.dict(),
            "device_signals":   {},
            "rule_hits":        [],
        }
        async with httpx.AsyncClient() as client:
            pd_r    = await client.post(f"{ray_serve_url}/score/pd",    json=evidence)
            fraud_r = await client.post(f"{ray_serve_url}/score/fraud", json=evidence)
            proxy_r = await client.post(f"{ray_serve_url}/score/proxy", json=evidence)
        return ModelScores(
            pd_score=pd_r.json()["score"],
            fraud_score=fraud_r.json()["score"],
            proxy_borrower_score=proxy_r.json()["score"],
            model_version="v1.0.0",
        )
    else:
        # Heuristic fallback for local dev
        bureau = (intake.bureau_score or 650)
        pd_base = max(0.02, min(0.95, (780 - bureau) / 400))
        fraud_base = 0.05 + graph_risk.graph_risk_score * 0.4
        proxy_base = graph_risk.graph_risk_score * 0.7
        return ModelScores(
            pd_score=round(pd_base, 4),
            fraud_score=round(fraud_base, 4),
            proxy_borrower_score=round(proxy_base, 4),
            model_version="heuristic-v0.1",
        )


def _build_evidence_bundle(
    case_id: str,
    application_id: str,
    intake: ApplicationIntake,
    graph_risk: GraphRiskOutput,
    scores: ModelScores,
    person_id: str,
) -> EvidenceBundle:
    timeline = [
        TimelineEvent(
            timestamp=intake.submitted_at,
            event=f"Application submitted for ${intake.loan_amount:,.0f} ({intake.loan_purpose})",
            source="LOS",
        )
    ]
    for party in graph_risk.related_parties:
        if party.recent_default and party.default_date:
            from datetime import datetime as dt
            timeline.append(TimelineEvent(
                timestamp=dt.combine(party.default_date, dt.min.time()).replace(tzinfo=UTC),
                event=f"Related party '{party.name}' defaulted on ${party.default_amount or 0:,.0f}",
                source="collections_system",
            ))
    timeline.sort(key=lambda e: e.timestamp)

    shared_attributes = []
    for party in graph_risk.related_parties:
        shared_attributes.extend(party.shared_attributes)
    shared_attributes = list(set(shared_attributes))

    return EvidenceBundle(
        case_id=case_id,
        application_id=application_id,
        applicant_name=intake.full_name,
        loan_amount=intake.loan_amount,
        submitted_at=intake.submitted_at,
        timeline=timeline,
        related_parties=graph_risk.related_parties,
        shared_attributes=shared_attributes,
        scores=scores,
        rule_hits=[],
        graph_risk=graph_risk,
        bureau_summary={
            "score": intake.bureau_score,
            "declared_income": intake.declared_income,
        },
        device_signals={
            "device_fingerprint": intake.device_fingerprint,
            "ip_address": intake.ip_address,
        },
        fund_flow_signals={
            "transferred_to_related_defaulter": graph_risk.fund_flow_to_defaulter
        },
        policy_references=["ECOA", "FCRA", "BSA/AML", "NIST-AI-RMF"],
    )


async def _store_case(
    db: asyncpg.Connection,
    case_id: str,
    application_id: str,
    classification: LLMCaseClassification,
    bundle: EvidenceBundle,
    policy_output: dict,
):
    import json
    from datetime import timedelta

    sla_deadline = datetime.now(UTC) + timedelta(hours=policy_output["sla_hours"])

    await db.execute("""
        INSERT INTO cases
            (case_id, application_id, case_type, priority,
             sla_deadline, status, llm_classification, evidence_bundle)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
    """, case_id, application_id, classification.case_type.value,
        policy_output["priority"], sla_deadline,
        "manual_review" if policy_output["human_review_required"] else "open",
        json.dumps(classification.dict()),
        json.dumps(bundle.dict(exclude={"rule_hits", "related_parties"})),
    )

    # Store rule hits
    for hit in bundle.rule_hits:
        await db.execute("""
            INSERT INTO rule_hits
                (hit_id, case_id, rule_code, rule_description, severity, evidence_data)
            VALUES ($1,$2,$3,$4,$5,$6)
        """, str(uuid.uuid4()), case_id, hit.rule_code,
            hit.description, hit.severity.value,
            json.dumps(hit.evidence))
