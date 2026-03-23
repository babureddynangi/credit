# api/schemas/models.py
# Pydantic data models for all platform entities

from __future__ import annotations
from datetime import datetime, date
from typing import Optional, List, Dict, Any
from uuid import UUID
from enum import Enum
from pydantic import BaseModel, Field, field_validator
import re


# ─── Enumerations ────────────────────────────────────────────────────────────

class OperationalStatus(str, Enum):
    PENDING          = "pending"
    APPROVED         = "approved"
    MANUAL_REVIEW    = "manual_review"
    HOLD             = "hold"
    FRAUD_ESCALATION = "fraud_escalation"
    DECLINED         = "declined"


class CreditPerformance(str, Enum):
    CURRENT     = "current"
    DELINQUENT  = "delinquent"
    DEFAULTED   = "defaulted"
    CHARGE_OFF  = "charge_off"


class FraudLabel(str, Enum):
    NONE                      = "none"
    RELATED_PARTY_RISK        = "related_party_risk"
    SUSPECTED_PROXY_BORROWER  = "suspected_proxy_borrower"
    SUSPECTED_IDENTITY_MISUSE = "suspected_identity_misuse"
    SUSPECTED_COORDINATED_FRAUD = "suspected_coordinated_fraud"
    CONFIRMED_FRAUD           = "confirmed_fraud"


class CaseType(str, Enum):
    INDEPENDENT_CREDIT_RISK     = "independent_credit_risk"
    RELATED_PARTY_RISK          = "related_party_risk"
    SUSPECTED_PROXY_BORROWER    = "suspected_proxy_borrower"
    SUSPECTED_IDENTITY_MISUSE   = "suspected_identity_misuse"
    SUSPECTED_COORDINATED_FRAUD = "suspected_coordinated_fraud"


class RuleSeverity(str, Enum):
    INFO     = "info"
    WARNING  = "warning"
    CRITICAL = "critical"


class ActorType(str, Enum):
    HUMAN       = "human"
    MODEL       = "model"
    RULE_ENGINE = "rule_engine"
    LLM         = "llm"
    SYSTEM      = "system"


# ─── Application Intake ──────────────────────────────────────────────────────

class ApplicationIntake(BaseModel):
    """Incoming loan application payload from LOS."""
    external_app_id:   str
    external_person_id: str
    full_name:         str
    dob:               date
    ssn_last4:         str = Field(..., min_length=4, max_length=4)
    loan_amount:       float = Field(..., gt=0, le=10_000_000)
    loan_purpose:      str
    declared_income:   float = Field(..., gt=0)
    address:           str
    phone:             Optional[str] = None
    email:             Optional[str] = None
    bank_account_hash: Optional[str] = None       # hashed, never plain
    device_fingerprint: Optional[str] = None
    ip_address:        Optional[str] = None
    bureau_score:      Optional[int] = Field(None, ge=300, le=850)
    submitted_at:      datetime

    @field_validator("ssn_last4")
    @classmethod
    def validate_ssn_last4(cls, v):
        if not re.match(r"^\d{4}$", v):
            raise ValueError("ssn_last4 must be exactly 4 digits")
        return v


# ─── Scores ──────────────────────────────────────────────────────────────────

class ModelScores(BaseModel):
    pd_score:             float = Field(..., ge=0, le=1, description="Probability of default")
    fraud_score:          float = Field(..., ge=0, le=1, description="Application fraud risk")
    proxy_borrower_score: float = Field(..., ge=0, le=1, description="Proxy/related-party misuse risk")
    model_version:        str


class RuleHit(BaseModel):
    rule_code:        str
    description:      str
    severity:         RuleSeverity
    evidence:         Dict[str, Any] = {}


class RuleEngineOutput(BaseModel):
    rule_hits:              List[RuleHit] = []
    critical_hit:           bool = False
    manual_review_required: bool = False


# ─── Graph Analysis ──────────────────────────────────────────────────────────

class RelatedParty(BaseModel):
    person_id:         str
    name:              str
    relationship_type: str
    shared_attributes: List[str] = []
    link_strength:     float = Field(..., ge=0, le=1)
    recent_default:    bool = False
    default_date:      Optional[date] = None
    default_amount:    Optional[float] = None


class GraphRiskOutput(BaseModel):
    related_parties:          List[RelatedParty] = []
    household_default_count:  int = 0
    shortest_path_to_defaulter: Optional[int] = None
    fund_flow_to_defaulter:   bool = False
    cluster_density:          float = 0.0
    graph_risk_score:         float = Field(..., ge=0, le=1)


# ─── Evidence Bundle ─────────────────────────────────────────────────────────

class TimelineEvent(BaseModel):
    timestamp: datetime
    event:     str
    source:    str


class EvidenceBundle(BaseModel):
    case_id:           str
    application_id:    str
    applicant_name:    str
    loan_amount:       float
    submitted_at:      datetime
    timeline:          List[TimelineEvent] = []
    related_parties:   List[RelatedParty] = []
    shared_attributes: List[str] = []
    scores:            ModelScores
    rule_hits:         List[RuleHit] = []
    graph_risk:        GraphRiskOutput
    bureau_summary:    Dict[str, Any] = {}
    device_signals:    Dict[str, Any] = {}
    fund_flow_signals: Dict[str, Any] = {}
    policy_references: List[str] = []


# ─── LLM Structured Output ───────────────────────────────────────────────────

class LLMCaseClassification(BaseModel):
    """Structured JSON output from LLM classification agent.
    Schema-bound via OpenAI structured outputs."""
    case_type:             CaseType
    confidence:            float = Field(..., ge=0, le=1)
    key_reasons:           List[str] = Field(..., min_length=1)
    missing_evidence:      List[str] = []
    recommended_action:    OperationalStatus
    human_review_required: bool
    fraud_label:           FraudLabel
    analyst_summary:       str = Field(..., max_length=2000)
    adverse_action_codes:  List[str] = []           # ECOA Reg B reason codes
    next_investigation_steps: List[str] = []


# ─── Decision Output ─────────────────────────────────────────────────────────

class DecisionOutput(BaseModel):
    application_id:        str
    case_id:               str
    operational_status:    OperationalStatus
    fraud_label:           FraudLabel
    credit_performance:    Optional[CreditPerformance] = None
    llm_classification:    LLMCaseClassification
    rule_hits:             List[RuleHit] = []
    scores:                ModelScores
    adverse_action_codes:  List[str] = []
    human_review_required: bool
    sla_hours:             int                        # hours until SLA breach
    decided_at:            datetime
    model_version:         str
    explanation:           str                        # ECOA-compliant plain-English reason


# ─── Human Review ────────────────────────────────────────────────────────────

class AnalystDecision(BaseModel):
    case_id:           str
    analyst_id:        str
    disposition:       str = Field(..., pattern="^(approved|declined|escalated|fraud_confirmed)$")
    reason_codes:      List[str] = Field(..., min_length=1)
    notes:             Optional[str] = None
    override_llm:      bool = False
    override_reason:   Optional[str] = None


# ─── Monitoring ──────────────────────────────────────────────────────────────

class ModelDriftAlert(BaseModel):
    model_name:    str
    metric:        str
    current_value: float
    threshold:     float
    alert_date:    date
    severity:      str
