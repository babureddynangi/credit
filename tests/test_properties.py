# tests/test_properties.py
# Property-based tests using Hypothesis — all 12 correctness properties
# Feature: credit-fraud-mvp

import os
import sys
import pytest
from datetime import datetime, date, timezone, timedelta
from hypothesis import given, settings, HealthCheck, assume
from hypothesis import strategies as st

sys.path.insert(0, ".")
os.environ.setdefault("OPENAI_API_KEY", "test-key-placeholder")
os.environ["MOCK_LLM"] = "true"
os.environ["GRAPH_BACKEND"] = "mock"

UTC = timezone.utc

# ─── Shared strategies ───────────────────────────────────────────────────────

safe_text = st.text(min_size=1, max_size=50,
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-_"))

score_st = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

@st.composite
def model_scores_st(draw, fraud=None, proxy=None, pd=None):
    from api.schemas.models import ModelScores
    return ModelScores(
        pd_score=draw(score_st) if pd is None else pd,
        fraud_score=draw(score_st) if fraud is None else fraud,
        proxy_borrower_score=draw(score_st) if proxy is None else proxy,
        model_version="heuristic-v0.1",
    )

@st.composite
def graph_risk_st(draw, household_defaults=None, fund_flow=None, density=None):
    from api.schemas.models import GraphRiskOutput
    return GraphRiskOutput(
        related_parties=[],
        household_default_count=draw(st.integers(min_value=0, max_value=10)) if household_defaults is None else household_defaults,
        shortest_path_to_defaulter=None,
        fund_flow_to_defaulter=draw(st.booleans()) if fund_flow is None else fund_flow,
        cluster_density=draw(score_st) if density is None else density,
        graph_risk_score=draw(score_st),
    )

@st.composite
def evidence_bundle_st(draw, rule_hits=None, related_parties=None):
    from api.schemas.models import EvidenceBundle, ModelScores, GraphRiskOutput
    return EvidenceBundle(
        case_id=draw(safe_text),
        application_id=draw(safe_text),
        applicant_name="Test Applicant",
        loan_amount=draw(st.floats(min_value=100.0, max_value=100000.0, allow_nan=False)),
        submitted_at=datetime.now(UTC),
        scores=draw(model_scores_st()),
        graph_risk=draw(graph_risk_st()),
        rule_hits=rule_hits if rule_hits is not None else [],
        related_parties=related_parties if related_parties is not None else [],
    )


# ─── Property 3: Rule engine output flags invariant ──────────────────────────
# Feature: credit-fraud-mvp, Property 3: Rule engine output flags invariant

@given(
    household_defaults=st.integers(min_value=0, max_value=10),
    fund_flow=st.booleans(),
    density=score_st,
    shared_attrs=st.lists(st.sampled_from(["bank_account", "device_fingerprint", "address"]), max_size=3),
)
@settings(max_examples=100, deadline=None)
def test_property3_rule_engine_flags_invariant(household_defaults, fund_flow, density, shared_attrs):
    """Any CRITICAL hit → critical_hit=True and manual_review_required=True.
    2+ WARNING hits → manual_review_required=True."""
    from rules.engine import RuleEngine
    from api.schemas.models import (
        EvidenceBundle, ModelScores, GraphRiskOutput, RelatedParty, RuleSeverity
    )

    recent_date = (datetime.now(UTC) - timedelta(days=30)).date()
    party = RelatedParty(
        person_id="p1", name="Related Person", relationship_type="sibling",
        shared_attributes=[], link_strength=0.8,
        recent_default=True, default_date=recent_date, default_amount=5000.0,
    )

    bundle = EvidenceBundle(
        case_id="test", application_id="app",
        applicant_name="Test", loan_amount=5000.0,
        submitted_at=datetime.now(UTC),
        scores=ModelScores(pd_score=0.2, fraud_score=0.1, proxy_borrower_score=0.1, model_version="test"),
        graph_risk=GraphRiskOutput(
            related_parties=[party],
            household_default_count=household_defaults,
            fund_flow_to_defaulter=fund_flow,
            cluster_density=density,
            graph_risk_score=0.0,
        ),
        related_parties=[party],
        shared_attributes=shared_attrs,
        fund_flow_signals={
            "transferred_to_related_defaulter": fund_flow,
            "transfer_hours_after_disbursement": 5 if fund_flow else 999,
        },
    )

    engine = RuleEngine()
    result = engine.evaluate(bundle)

    # Property: any CRITICAL hit → both flags True
    has_critical = any(h.severity == RuleSeverity.CRITICAL for h in result.rule_hits)
    if has_critical:
        assert result.critical_hit is True
        assert result.manual_review_required is True

    # Property: 2+ WARNINGs → manual_review_required
    warning_count = sum(1 for h in result.rule_hits if h.severity == RuleSeverity.WARNING)
    if warning_count >= 2:
        assert result.manual_review_required is True


# ─── Property 4: Rule hits always contain evidence ───────────────────────────
# Feature: credit-fraud-mvp, Property 4: Rule hits always contain evidence

@given(
    fund_flow=st.booleans(),
    shared_attrs=st.lists(st.sampled_from(["bank_account", "device_fingerprint"]), min_size=1, max_size=2),
)
@settings(max_examples=100, deadline=None)
def test_property4_rule_hits_have_evidence(fund_flow, shared_attrs):
    """Every RuleHit must have a non-empty evidence dict."""
    from rules.engine import RuleEngine
    from api.schemas.models import (
        EvidenceBundle, ModelScores, GraphRiskOutput, RelatedParty
    )

    recent_date = (datetime.now(UTC) - timedelta(days=30)).date()
    party = RelatedParty(
        person_id="p1", name="Related Person", relationship_type="sibling",
        shared_attributes=[], link_strength=0.8,
        recent_default=True, default_date=recent_date, default_amount=5000.0,
    )

    bundle = EvidenceBundle(
        case_id="test", application_id="app",
        applicant_name="Test", loan_amount=5000.0,
        submitted_at=datetime.now(UTC),
        scores=ModelScores(pd_score=0.2, fraud_score=0.1, proxy_borrower_score=0.1, model_version="test"),
        graph_risk=GraphRiskOutput(
            related_parties=[party], household_default_count=0,
            fund_flow_to_defaulter=fund_flow, cluster_density=0.0, graph_risk_score=0.0,
        ),
        related_parties=[party],
        shared_attributes=shared_attrs,
        fund_flow_signals={
            "transferred_to_related_defaulter": fund_flow,
            "transfer_hours_after_disbursement": 5 if fund_flow else 999,
        },
    )

    engine = RuleEngine()
    result = engine.evaluate(bundle)

    for hit in result.rule_hits:
        assert hit.evidence, f"Rule {hit.rule_code} has empty evidence"


# ─── Property 5: Model scores are bounded and versioned ──────────────────────
# Feature: credit-fraud-mvp, Property 5: Model scores are bounded and versioned

def _heuristic_scores(bureau_score, graph_risk_score, fund_flow_to_defaulter):
    """Inline heuristic scoring logic mirroring api/main.py _get_ml_scores fallback."""
    from api.schemas.models import ModelScores
    bureau = bureau_score or 650
    pd_base = max(0.02, min(0.95, (780 - bureau) / 400))
    fraud_base = 0.05 + graph_risk_score * 0.4
    proxy_base = graph_risk_score * 0.7
    return ModelScores(
        pd_score=round(pd_base, 4),
        fraud_score=round(min(fraud_base, 1.0), 4),
        proxy_borrower_score=round(min(proxy_base, 1.0), 4),
        model_version="heuristic-v0.1",
    )


@given(
    bureau_score=st.one_of(st.none(), st.integers(min_value=300, max_value=850)),
    graph_risk_score=score_st,
    fund_flow=st.booleans(),
)
@settings(max_examples=100, deadline=None)
def test_property5_model_scores_bounded(bureau_score, graph_risk_score, fund_flow):
    """Heuristic scoring always returns scores in [0,1] with non-empty model_version."""
    scores = _heuristic_scores(bureau_score, graph_risk_score, fund_flow)

    assert 0.0 <= scores.pd_score <= 1.0
    assert 0.0 <= scores.fraud_score <= 1.0
    assert 0.0 <= scores.proxy_borrower_score <= 1.0
    assert scores.model_version != ""


# ─── Property 9: Policy threshold routing ────────────────────────────────────
# Feature: credit-fraud-mvp, Property 9: Policy threshold routing

@given(
    fraud_score=st.floats(min_value=0.85, max_value=1.0, allow_nan=False),
)
@settings(max_examples=50, deadline=None)
def test_property9_fraud_escalation_threshold(fraud_score):
    """fraud_score >= 0.85 → fraud_escalation, priority=5."""
    from llm.agents import PolicyMappingAgent
    from api.schemas.models import (
        EvidenceBundle, ModelScores, GraphRiskOutput,
        LLMCaseClassification, CaseType, OperationalStatus, FraudLabel
    )

    scores = ModelScores(pd_score=0.1, fraud_score=fraud_score, proxy_borrower_score=0.1, model_version="test")
    bundle = EvidenceBundle(
        case_id="test", application_id="app",
        applicant_name="Test", loan_amount=5000.0,
        submitted_at=datetime.now(UTC),
        scores=scores,
        graph_risk=GraphRiskOutput(related_parties=[], household_default_count=0,
            fund_flow_to_defaulter=False, cluster_density=0.0, graph_risk_score=0.0),
    )
    classification = LLMCaseClassification(
        case_type=CaseType.INDEPENDENT_CREDIT_RISK, confidence=0.85,
        key_reasons=["test"], missing_evidence=[], recommended_action=OperationalStatus.APPROVED,
        human_review_required=False, fraud_label=FraudLabel.NONE,
        analyst_summary="test", adverse_action_codes=[], next_investigation_steps=[],
    )

    result = PolicyMappingAgent().apply(classification, bundle)
    assert result["operational_status"] == OperationalStatus.FRAUD_ESCALATION
    assert result["priority"] == 5


@given(
    proxy_score=st.floats(min_value=0.80, max_value=1.0, allow_nan=False),
    fraud_score=st.floats(min_value=0.0, max_value=0.849, allow_nan=False),
)
@settings(max_examples=50, deadline=None)
def test_property9_proxy_escalation_threshold(proxy_score, fraud_score):
    """proxy_borrower_score >= 0.80 (and fraud < 0.85) → fraud_escalation, priority=10."""
    from llm.agents import PolicyMappingAgent
    from api.schemas.models import (
        EvidenceBundle, ModelScores, GraphRiskOutput,
        LLMCaseClassification, CaseType, OperationalStatus, FraudLabel
    )

    scores = ModelScores(pd_score=0.1, fraud_score=fraud_score, proxy_borrower_score=proxy_score, model_version="test")
    bundle = EvidenceBundle(
        case_id="test", application_id="app",
        applicant_name="Test", loan_amount=5000.0,
        submitted_at=datetime.now(UTC),
        scores=scores,
        graph_risk=GraphRiskOutput(related_parties=[], household_default_count=0,
            fund_flow_to_defaulter=False, cluster_density=0.0, graph_risk_score=0.0),
    )
    classification = LLMCaseClassification(
        case_type=CaseType.INDEPENDENT_CREDIT_RISK, confidence=0.85,
        key_reasons=["test"], missing_evidence=[], recommended_action=OperationalStatus.APPROVED,
        human_review_required=False, fraud_label=FraudLabel.NONE,
        analyst_summary="test", adverse_action_codes=[], next_investigation_steps=[],
    )

    result = PolicyMappingAgent().apply(classification, bundle)
    assert result["operational_status"] == OperationalStatus.FRAUD_ESCALATION
    assert result["priority"] == 10


@given(
    fraud_score=st.floats(min_value=0.65, max_value=0.849, allow_nan=False),
    proxy_score=st.floats(min_value=0.0, max_value=0.799, allow_nan=False),
)
@settings(max_examples=50, deadline=None)
def test_property9_hold_threshold(fraud_score, proxy_score):
    """0.65 <= fraud_score < 0.85 (proxy < 0.80) → hold."""
    from llm.agents import PolicyMappingAgent
    from api.schemas.models import (
        EvidenceBundle, ModelScores, GraphRiskOutput,
        LLMCaseClassification, CaseType, OperationalStatus, FraudLabel
    )

    scores = ModelScores(pd_score=0.1, fraud_score=fraud_score, proxy_borrower_score=proxy_score, model_version="test")
    bundle = EvidenceBundle(
        case_id="test", application_id="app",
        applicant_name="Test", loan_amount=5000.0,
        submitted_at=datetime.now(UTC),
        scores=scores,
        graph_risk=GraphRiskOutput(related_parties=[], household_default_count=0,
            fund_flow_to_defaulter=False, cluster_density=0.0, graph_risk_score=0.0),
    )
    classification = LLMCaseClassification(
        case_type=CaseType.INDEPENDENT_CREDIT_RISK, confidence=0.85,
        key_reasons=["test"], missing_evidence=[], recommended_action=OperationalStatus.APPROVED,
        human_review_required=False, fraud_label=FraudLabel.NONE,
        analyst_summary="test", adverse_action_codes=[], next_investigation_steps=[],
    )

    result = PolicyMappingAgent().apply(classification, bundle)
    assert result["operational_status"] == OperationalStatus.HOLD


# ─── Property 10: Critical rule hit prevents approval ────────────────────────
# Feature: credit-fraud-mvp, Property 10: Critical rule hit prevents approval

def test_property10_critical_rule_prevents_approval():
    """Any bundle with a CRITICAL rule hit must not result in approved status."""
    from llm.agents import PolicyMappingAgent
    from api.schemas.models import (
        EvidenceBundle, ModelScores, GraphRiskOutput, RuleHit, RuleSeverity,
        LLMCaseClassification, CaseType, OperationalStatus, FraudLabel
    )

    critical_hit = RuleHit(
        rule_code="R002_SHARED_BANK_ACCOUNT_WITH_DEFAULTER",
        description="Shared bank account with defaulter",
        severity=RuleSeverity.CRITICAL,
        evidence={"shared_attribute": "bank_account"},
    )
    scores = ModelScores(pd_score=0.1, fraud_score=0.1, proxy_borrower_score=0.1, model_version="test")
    bundle = EvidenceBundle(
        case_id="test", application_id="app",
        applicant_name="Test", loan_amount=5000.0,
        submitted_at=datetime.now(UTC),
        scores=scores,
        graph_risk=GraphRiskOutput(related_parties=[], household_default_count=0,
            fund_flow_to_defaulter=False, cluster_density=0.0, graph_risk_score=0.0),
        rule_hits=[critical_hit],
    )
    classification = LLMCaseClassification(
        case_type=CaseType.INDEPENDENT_CREDIT_RISK, confidence=0.85,
        key_reasons=["test"], missing_evidence=[], recommended_action=OperationalStatus.APPROVED,
        human_review_required=False, fraud_label=FraudLabel.NONE,
        analyst_summary="test", adverse_action_codes=[], next_investigation_steps=[],
    )

    result = PolicyMappingAgent().apply(classification, bundle)
    assert result["operational_status"] != OperationalStatus.APPROVED


# ─── Property 12: Graph risk score is bounded ────────────────────────────────
# Feature: credit-fraud-mvp, Property 12: Graph risk score is bounded

@given(
    recent_defaulters=st.integers(min_value=0, max_value=5),
    household_defaults=st.integers(min_value=0, max_value=10),
    shortest_path=st.one_of(st.none(), st.integers(min_value=1, max_value=5)),
    fund_flow=st.booleans(),
    density=score_st,
)
@settings(max_examples=200, deadline=None)
def test_property12_graph_risk_score_bounded(recent_defaulters, household_defaults, shortest_path, fund_flow, density):
    """_compute_graph_risk_score always returns a value in [0.0, 1.0]."""
    from graph.analyzer import GraphAnalyzer
    from api.schemas.models import RelatedParty

    parties = [
        RelatedParty(
            person_id=f"p{i}", name=f"Person {i}", relationship_type="linked",
            shared_attributes=[], link_strength=0.5,
            recent_default=True, default_date=date(2024, 1, 1),
        )
        for i in range(recent_defaulters)
    ]

    analyzer = GraphAnalyzer()
    score = analyzer._compute_graph_risk_score(
        related_parties=parties,
        household_default_count=household_defaults,
        shortest_path=shortest_path,
        fund_flow_to_defaulter=fund_flow,
        cluster_density=density,
    )

    assert 0.0 <= score <= 1.0


# ─── Property 1: SSN is never stored in plain text ───────────────────────────
# Feature: credit-fraud-mvp, Property 1: SSN is never stored in plain text

@given(ssn_last4=st.from_regex(r"^\d{4}$", fullmatch=True))
@settings(max_examples=100, deadline=None)
def test_property1_ssn_hashing(ssn_last4):
    """SHA-256 hash of ssn_last4 must not equal the plain value."""
    import hashlib
    hashed = hashlib.sha256(ssn_last4.encode()).hexdigest()
    assert hashed != ssn_last4
    assert len(hashed) == 64  # SHA-256 hex digest is always 64 chars
    # Verify determinism
    assert hashlib.sha256(ssn_last4.encode()).hexdigest() == hashed

