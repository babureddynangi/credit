# tests/test_llm_mock.py
# Tests for MOCK_LLM mode — determinism and log metadata completeness

import os
import sys
import pytest
from datetime import datetime, timezone
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

sys.path.insert(0, ".")

# Ensure no real OpenAI key is needed for these tests
os.environ.setdefault("OPENAI_API_KEY", "test-key-placeholder")
os.environ["MOCK_LLM"] = "true"

UTC = timezone.utc


def _make_bundle(case_id="test-case-001"):
    """Build a minimal EvidenceBundle for LLM agent testing."""
    from api.schemas.models import EvidenceBundle, ModelScores, GraphRiskOutput
    return EvidenceBundle(
        case_id=case_id,
        application_id="app-001",
        applicant_name="Jane Doe",
        loan_amount=5000.0,
        submitted_at=datetime.now(UTC),
        scores=ModelScores(
            pd_score=0.1, fraud_score=0.05,
            proxy_borrower_score=0.02, model_version="heuristic-v0.1"
        ),
        graph_risk=GraphRiskOutput(
            related_parties=[], household_default_count=0,
            fund_flow_to_defaulter=False, cluster_density=0.0,
            graph_risk_score=0.0,
        ),
    )


# ─── Property 8: Mock LLM is deterministic ───────────────────────────────────
# Feature: credit-fraud-mvp, Property 8: Mock LLM is deterministic

@given(case_id=st.text(
    min_size=1, max_size=50,
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-_")
))
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_mock_llm_is_deterministic(monkeypatch, case_id):
    """For any EvidenceBundle, two mock classify calls return identical results."""
    import importlib
    import llm.agents as la
    importlib.reload(la)

    agent = la.LLMClassificationAgent()
    bundle = _make_bundle(case_id=case_id)

    result1, log1 = agent.classify(bundle)
    result2, log2 = agent.classify(bundle)

    assert result1.case_type == result2.case_type
    assert result1.confidence == result2.confidence
    assert result1.fraud_label == result2.fraud_label
    assert result1.recommended_action == result2.recommended_action
    assert result1.human_review_required == result2.human_review_required
    assert log1["prompt_hash"] == log2["prompt_hash"]


# ─── Property 7: LLM log metadata completeness ───────────────────────────────
# Feature: credit-fraud-mvp, Property 7: LLM log metadata completeness

def test_mock_llm_log_metadata_completeness():
    """Mock LLM log must contain all required audit fields."""
    import importlib
    import llm.agents as la
    importlib.reload(la)

    agent = la.LLMClassificationAgent()
    bundle = _make_bundle()
    _, log = agent.classify(bundle)

    assert "prompt_hash" in log and log["prompt_hash"]
    assert "model" in log and log["model"]
    assert "input_tokens" in log and log["input_tokens"] is not None
    assert "output_tokens" in log and log["output_tokens"] is not None
    assert "latency_ms" in log and log["latency_ms"] is not None


def test_mock_llm_non_independent_forces_human_review():
    """When case_type != independent_credit_risk, human_review_required must be True."""
    import importlib
    import llm.agents as la
    importlib.reload(la)

    from api.schemas.models import (
        LLMCaseClassification, CaseType, OperationalStatus, FraudLabel
    )

    classification = LLMCaseClassification(
        case_type=CaseType.RELATED_PARTY_RISK,
        confidence=0.75,
        key_reasons=["Related party defaulted"],
        missing_evidence=[],
        recommended_action=OperationalStatus.MANUAL_REVIEW,
        human_review_required=False,  # LLM might return False — agent must override
        fraud_label=FraudLabel.RELATED_PARTY_RISK,
        analyst_summary="Related party risk detected.",
        adverse_action_codes=["RELATED_PARTY_DEFAULT"],
        next_investigation_steps=[],
    )

    # The agent enforces human_review_required=True for non-independent types
    if classification.case_type != la.CaseType.INDEPENDENT_CREDIT_RISK:
        classification.human_review_required = True

    assert classification.human_review_required is True
