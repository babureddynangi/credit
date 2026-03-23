# llm/agents.py
# LLM investigation agents using OpenAI structured outputs
# Compliant with NIST AI RMF: all prompts and outputs logged, human review enforced

from __future__ import annotations
import os
import json
import hashlib
import logging
import time
from typing import Optional
from datetime import datetime, timezone

import openai
from openai import OpenAI

from api.schemas.models import (
    EvidenceBundle, LLMCaseClassification, CaseType, OperationalStatus,
    FraudLabel, RuleSeverity
)

logger = logging.getLogger(__name__)
UTC = timezone.utc

MOCK_LLM = os.getenv("MOCK_LLM", "false").lower() == "true"

# Lazy client — only instantiated when actually needed (not in mock mode)
_openai_client = None

def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    return _openai_client

# ─── JSON Schema for OpenAI Structured Output ────────────────────────────────

LLM_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "case_type": {
            "type": "string",
            "enum": [
                "independent_credit_risk",
                "related_party_risk",
                "suspected_proxy_borrower",
                "suspected_identity_misuse",
                "suspected_coordinated_fraud",
            ]
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "key_reasons": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 10,
        },
        "missing_evidence": {
            "type": "array",
            "items": {"type": "string"},
        },
        "recommended_action": {
            "type": "string",
            "enum": ["approved", "manual_review", "hold", "fraud_escalation", "declined"],
        },
        "human_review_required": {"type": "boolean"},
        "fraud_label": {
            "type": "string",
            "enum": [
                "none",
                "related_party_risk",
                "suspected_proxy_borrower",
                "suspected_identity_misuse",
                "suspected_coordinated_fraud",
                "confirmed_fraud",
            ]
        },
        "analyst_summary": {"type": "string", "maxLength": 2000},
        "adverse_action_codes": {
            "type": "array",
            "items": {"type": "string"},
        },
        "next_investigation_steps": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "case_type", "confidence", "key_reasons", "missing_evidence",
        "recommended_action", "human_review_required", "fraud_label",
        "analyst_summary", "adverse_action_codes", "next_investigation_steps"
    ],
    "additionalProperties": False,
}


# ─── System Prompts ───────────────────────────────────────────────────────────

CLASSIFICATION_SYSTEM_PROMPT = """You are a credit fraud investigation AI assistant at a regulated US lending institution.

Your role:
- Analyze pre-assembled evidence bundles to classify potential fraud and credit risk cases
- Produce structured, evidence-grounded classifications — never invent facts not in the bundle
- Support human analysts; NEVER make final approval or denial decisions autonomously
- Comply with ECOA (Equal Credit Opportunity Act), FCRA, and BSA/AML requirements

Key principles:
1. Ground every key_reason in a specific fact from the evidence bundle
2. Flag missing evidence honestly — incomplete evidence should trigger human review
3. For adverse_action_codes, use CFPB standard codes (1-25) for credit reasons;
   use internal codes RELATED_PARTY_DEFAULT, PROXY_BORROWER_RISK, FUND_FLOW_ANOMALY for fraud reasons
4. human_review_required must be TRUE whenever:
   - confidence < 0.70
   - case_type is anything other than independent_credit_risk
   - Any critical rule hit is present
   - Fund flow anomaly is detected
5. analyst_summary must be plain English, suitable for an audit trail, max 2000 chars
6. Do NOT mention protected class characteristics (race, gender, national origin, age, religion,
   marital status, receipt of public assistance) in any field — ECOA compliance

Classification guide:
- independent_credit_risk: No significant related-party signals; ordinary credit evaluation
- related_party_risk: Family/household member recently defaulted; possible but not confirmed misuse
- suspected_proxy_borrower: Strong evidence named borrower is not real beneficiary/controller
- suspected_identity_misuse: Evidence of SSN/identity theft or unauthorized application
- suspected_coordinated_fraud: Multiple related parties appear to be orchestrating fraud ring
"""


# ─── Agent Classes ────────────────────────────────────────────────────────────

class IntakeAgent:
    """
    Validates evidence bundle completeness.
    Returns a list of missing required fields before LLM classification.
    """
    REQUIRED_FIELDS = [
        "applicant_name", "loan_amount", "submitted_at",
        "scores", "rule_hits",
    ]
    RECOMMENDED_FIELDS = [
        "timeline", "related_parties", "device_signals", "fund_flow_signals",
        "bureau_summary",
    ]

    def validate(self, bundle: EvidenceBundle) -> dict:
        missing_required = []
        missing_recommended = []

        for field in self.REQUIRED_FIELDS:
            val = getattr(bundle, field, None)
            if val is None or val == [] or val == {}:
                missing_required.append(field)

        for field in self.RECOMMENDED_FIELDS:
            val = getattr(bundle, field, None)
            if val is None or val == [] or val == {}:
                missing_recommended.append(field)

        return {
            "complete": len(missing_required) == 0,
            "missing_required": missing_required,
            "missing_recommended": missing_recommended,
        }


class LLMClassificationAgent:
    """
    Core LLM agent: classifies cases using OpenAI structured outputs.
    All prompts and outputs are logged for NIST AI RMF traceability.
    """

    MODEL = "gpt-4o-2024-08-06"  # supports structured outputs natively

    def classify(
        self,
        bundle: EvidenceBundle,
        db_session=None,
    ) -> tuple[LLMCaseClassification, dict]:
        """
        Returns (LLMCaseClassification, log_metadata).
        log_metadata contains prompt_hash, token counts, latency_ms for audit.
        """
        if MOCK_LLM:
            return self._mock_classify(bundle), self._mock_log(bundle)

        prompt = self._build_prompt(bundle)
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()

        start = time.time()
        try:
            response = _get_openai_client().chat.completions.create(
                model=self.MODEL,
                messages=[
                    {"role": "system", "content": CLASSIFICATION_SYSTEM_PROMPT},
                    {"role": "user",   "content": prompt},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "case_classification",
                        "strict": True,
                        "schema": LLM_OUTPUT_SCHEMA,
                    }
                },
                temperature=0,       # deterministic for regulated decisions
                max_tokens=1500,
            )
        except openai.APIError as e:
            logger.error(f"OpenAI API error for case {bundle.case_id}: {e}")
            raise

        latency_ms = int((time.time() - start) * 1000)
        raw_output = response.choices[0].message.content
        parsed     = json.loads(raw_output)

        classification = LLMCaseClassification(**parsed)

        # NIST AI RMF: enforce human review for high-risk classifications
        if classification.case_type != CaseType.INDEPENDENT_CREDIT_RISK:
            classification.human_review_required = True

        log_meta = {
            "prompt_hash":    prompt_hash,
            "model":          self.MODEL,
            "input_tokens":   response.usage.prompt_tokens,
            "output_tokens":  response.usage.completion_tokens,
            "latency_ms":     latency_ms,
            "case_id":        bundle.case_id,
            "structured_output": parsed,
        }

        return classification, log_meta

    def _mock_classify(self, bundle: EvidenceBundle) -> LLMCaseClassification:
        """Deterministic mock classification — used when MOCK_LLM=true."""
        return LLMCaseClassification(
            case_type=CaseType.INDEPENDENT_CREDIT_RISK,
            confidence=0.85,
            key_reasons=["Mock classification: no real LLM call made"],
            missing_evidence=[],
            recommended_action=OperationalStatus.APPROVED,
            human_review_required=False,
            fraud_label=FraudLabel.NONE,
            analyst_summary=(
                f"[MOCK] Case {bundle.case_id} — Independent Credit Risk (85% confidence). "
                f"No fraud signals detected. Action: approved."
            ),
            adverse_action_codes=[],
            next_investigation_steps=[],
        )

    def _mock_log(self, bundle: EvidenceBundle) -> dict:
        """Deterministic mock log metadata — used when MOCK_LLM=true."""
        prompt = self._build_prompt(bundle)
        return {
            "prompt_hash":       hashlib.sha256(prompt.encode()).hexdigest(),
            "model":             "mock-llm",
            "input_tokens":      len(prompt) // 4,   # rough token estimate
            "output_tokens":     50,
            "latency_ms":        0,
            "case_id":           bundle.case_id,
            "structured_output": {},
        }

    def _build_prompt(self, bundle: EvidenceBundle) -> str:
        """Builds the evidence-grounded prompt from the bundle."""
        rule_summary = "\n".join(
            f"  - [{h.severity.upper()}] {h.rule_code}: {h.description}"
            for h in bundle.rule_hits
        ) or "  No rule hits."

        related_summary = "\n".join(
            f"  - {p.name} ({p.relationship_type}): "
            f"shared={p.shared_attributes}, "
            f"recent_default={p.recent_default}"
            + (f", defaulted {p.default_date} on ${p.default_amount:,.0f}" if p.recent_default else "")
            for p in bundle.related_parties
        ) or "  No related parties found."

        timeline_summary = "\n".join(
            f"  [{e.timestamp.strftime('%Y-%m-%d')}] {e.event} (source: {e.source})"
            for e in bundle.timeline
        ) or "  No timeline events."

        return f"""EVIDENCE BUNDLE FOR CASE {bundle.case_id}
================================================================================
APPLICANT:     {bundle.applicant_name}
LOAN AMOUNT:   ${bundle.loan_amount:,.2f}
SUBMITTED:     {bundle.submitted_at.strftime('%Y-%m-%d %H:%M UTC')}

SCORES:
  PD Score (default risk):      {bundle.scores.pd_score:.2%}
  Fraud Score:                  {bundle.scores.fraud_score:.2%}
  Proxy Borrower Score:         {bundle.scores.proxy_borrower_score:.2%}
  Graph Risk Score:             {bundle.graph_risk.graph_risk_score:.2%}

RULE ENGINE HITS:
{rule_summary}

RELATED PARTIES:
{related_summary}

SHARED ATTRIBUTES: {', '.join(bundle.shared_attributes) or 'none'}

GRAPH METRICS:
  Household defaults (12mo):    {bundle.graph_risk.household_default_count}
  Shortest path to defaulter:   {bundle.graph_risk.shortest_path_to_defaulter or 'N/A'}
  Fund flow to defaulter:       {bundle.graph_risk.fund_flow_to_defaulter}
  Cluster density:              {bundle.graph_risk.cluster_density:.2%}

DEVICE / IDENTITY SIGNALS:
{json.dumps(bundle.device_signals, indent=2) or '  None'}

FUND FLOW SIGNALS:
{json.dumps(bundle.fund_flow_signals, indent=2) or '  None'}

TIMELINE:
{timeline_summary}

POLICY REFERENCES: {', '.join(bundle.policy_references) or 'None'}
================================================================================
Based solely on the evidence above, classify this case and provide structured output.
Do NOT invent facts not present in this bundle.
"""


class PolicyMappingAgent:
    """
    Maps LLM classification + rule hits to:
    - Final operational status (approve/review/hold/escalate)
    - Adverse action reason codes (ECOA Reg B compliance)
    - SLA priority
    """

    # Score thresholds — tune with validation data
    FRAUD_SCORE_ESCALATE     = 0.85
    FRAUD_SCORE_HOLD         = 0.65
    PD_SCORE_DECLINE         = 0.75
    PROXY_SCORE_ESCALATE     = 0.80

    def apply(
        self,
        classification: LLMCaseClassification,
        bundle: EvidenceBundle,
    ) -> dict:
        status   = classification.recommended_action
        priority = 50

        # Hard overrides based on model scores
        if bundle.scores.fraud_score >= self.FRAUD_SCORE_ESCALATE:
            status   = OperationalStatus.FRAUD_ESCALATION
            priority = 5
        elif bundle.scores.proxy_borrower_score >= self.PROXY_SCORE_ESCALATE:
            status   = OperationalStatus.FRAUD_ESCALATION
            priority = 10
        elif bundle.scores.fraud_score >= self.FRAUD_SCORE_HOLD:
            status   = OperationalStatus.HOLD
            priority = 20
        elif bundle.scores.pd_score >= self.PD_SCORE_DECLINE:
            status   = OperationalStatus.MANUAL_REVIEW
            priority = 30

        # Critical rule hit always escalates to manual review minimum
        has_critical = any(h.severity == RuleSeverity.CRITICAL for h in bundle.rule_hits)
        if has_critical and status == OperationalStatus.APPROVED:
            status   = OperationalStatus.MANUAL_REVIEW
            priority = 25

        # Build adverse action codes (ECOA Reg B)
        aa_codes = list(classification.adverse_action_codes)
        if bundle.scores.pd_score > 0.5:
            aa_codes.append("1")   # Derogatory credit history equivalent
        if bundle.graph_risk.household_default_count > 0:
            aa_codes.append("RELATED_PARTY_DEFAULT")
        if bundle.scores.proxy_borrower_score > 0.5:
            aa_codes.append("PROXY_BORROWER_RISK")
        if bundle.graph_risk.fund_flow_to_defaulter:
            aa_codes.append("FUND_FLOW_ANOMALY")

        # SLA hours based on priority
        sla_hours = {5: 2, 10: 4, 20: 8, 25: 12, 30: 24, 50: 48}.get(priority, 48)

        return {
            "operational_status":    status,
            "priority":              priority,
            "adverse_action_codes":  list(set(aa_codes)),
            "sla_hours":             sla_hours,
            "human_review_required": classification.human_review_required or has_critical,
        }


class LLMSummaryAgent:
    """Generates a concise ECOA-compliant plain-English explanation for analysts."""

    def summarize(self, classification: LLMCaseClassification, bundle: EvidenceBundle) -> str:
        top_reasons = "; ".join(classification.key_reasons[:3])
        return (
            f"Case {bundle.case_id} — {classification.case_type.replace('_', ' ').title()} "
            f"(confidence {classification.confidence:.0%}). "
            f"Key factors: {top_reasons}. "
            f"Action: {classification.recommended_action.replace('_', ' ')}. "
            f"Human review required: {classification.human_review_required}."
        )
