# tests/test_rules_engine.py
# Unit tests for the rule engine — covers Dan/sister scenario and edge cases

import pytest
from datetime import datetime, date, timezone, timedelta
from unittest.mock import MagicMock

import sys
sys.path.insert(0, ".")

from rules.engine import RuleEngine
from api.schemas.models import (
    EvidenceBundle, ModelScores, GraphRiskOutput, RelatedParty,
    RuleHit, RuleSeverity, TimelineEvent,
)

UTC = timezone.utc

def make_bundle(overrides: dict = {}) -> EvidenceBundle:
    """Build a minimal valid evidence bundle for testing."""
    defaults = dict(
        case_id="test-001",
        application_id="app-001",
        applicant_name="Test Applicant",
        loan_amount=5000.0,
        submitted_at=datetime.now(UTC),
        timeline=[],
        related_parties=[],
        shared_attributes=[],
        scores=ModelScores(pd_score=0.2, fraud_score=0.1, proxy_borrower_score=0.1, model_version="test"),
        rule_hits=[],
        graph_risk=GraphRiskOutput(
            related_parties=[],
            household_default_count=0,
            shortest_path_to_defaulter=None,
            fund_flow_to_defaulter=False,
            cluster_density=0.0,
            graph_risk_score=0.0,
        ),
        bureau_summary={},
        device_signals={},
        fund_flow_signals={},
        policy_references=[],
    )
    defaults.update(overrides)
    return EvidenceBundle(**defaults)


class TestRuleEngine:
    engine = RuleEngine()

    def test_clean_application_no_hits(self):
        """Clean application with no risk signals should produce no rule hits."""
        bundle = make_bundle()
        result = self.engine.evaluate(bundle)
        assert result.rule_hits == []
        assert result.critical_hit is False
        assert result.manual_review_required is False

    def test_r001_household_default_within_window(self):
        """R001 fires when related party defaulted within 90 days."""
        recent_default_date = (datetime.now(UTC) - timedelta(days=47)).date()
        party = RelatedParty(
            person_id="dan-001",
            name="Dan Johnson",
            relationship_type="sibling",
            shared_attributes=[],
            link_strength=0.8,
            recent_default=True,
            default_date=recent_default_date,
            default_amount=5000.0,
        )
        bundle = make_bundle({"related_parties": [party]})
        result = self.engine.evaluate(bundle)
        codes = [h.rule_code for h in result.rule_hits]
        assert "R001_HOUSEHOLD_RECENT_DEFAULT" in codes
        assert result.manual_review_required is False  # warning only, need 2 warnings

    def test_r001_default_outside_window_no_hit(self):
        """R001 should NOT fire if default was more than 90 days ago."""
        old_date = (datetime.now(UTC) - timedelta(days=95)).date()
        party = RelatedParty(
            person_id="old-001",
            name="Old Defaulter",
            relationship_type="sibling",
            shared_attributes=[],
            link_strength=0.5,
            recent_default=True,
            default_date=old_date,
            default_amount=1000.0,
        )
        bundle = make_bundle({"related_parties": [party]})
        result = self.engine.evaluate(bundle)
        codes = [h.rule_code for h in result.rule_hits]
        assert "R001_HOUSEHOLD_RECENT_DEFAULT" not in codes

    def test_r002_shared_bank_account_with_defaulter(self):
        """R002 fires when shared bank account + recent defaulter — should be CRITICAL."""
        party = RelatedParty(
            person_id="dan-001", name="Dan Johnson", relationship_type="sibling",
            shared_attributes=[], link_strength=0.9, recent_default=True,
            default_date=(datetime.now(UTC) - timedelta(days=30)).date(),
            default_amount=5000.0,
        )
        bundle = make_bundle({
            "related_parties": [party],
            "shared_attributes": ["bank_account"],
        })
        result = self.engine.evaluate(bundle)
        codes = [h.rule_code for h in result.rule_hits]
        assert "R002_SHARED_BANK_ACCOUNT_WITH_DEFAULTER" in codes
        assert result.critical_hit is True
        assert result.manual_review_required is True

    def test_r003_funds_transferred_to_defaulter(self):
        """R003 fires for fund transfer to related defaulter within 7 days."""
        bundle = make_bundle({
            "fund_flow_signals": {
                "transferred_to_related_defaulter": True,
                "transfer_hours_after_disbursement": 20,
            }
        })
        result = self.engine.evaluate(bundle)
        codes = [h.rule_code for h in result.rule_hits]
        assert "R003_FUNDS_TRANSFERRED_TO_RELATED_DEFAULTER" in codes
        assert result.critical_hit is True

    def test_r008_shared_device_with_defaulter(self):
        """R008 fires on shared device fingerprint with recent defaulter."""
        party = RelatedParty(
            person_id="dan-001", name="Dan Johnson", relationship_type="sibling",
            shared_attributes=[], link_strength=0.9, recent_default=True,
            default_date=(datetime.now(UTC) - timedelta(days=47)).date(),
        )
        bundle = make_bundle({
            "related_parties": [party],
            "shared_attributes": ["device_fingerprint"],
        })
        result = self.engine.evaluate(bundle)
        codes = [h.rule_code for h in result.rule_hits]
        assert "R008_SHARED_DEVICE_WITH_RECENT_DEFAULTER" in codes
        assert result.critical_hit is True

    def test_dan_sister_proxy_scenario_full(self):
        """
        Full Dan-sister proxy borrower scenario:
        - Dan defaulted 47 days ago
        - Sister applied same amount
        - Shared device fingerprint
        - Funds transferred to Dan within 24h
        Should trigger: R001 + R008 + R003, critical=True, manual_review=True
        """
        dan = RelatedParty(
            person_id="dan-001",
            name="Dan Johnson",
            relationship_type="sibling",
            shared_attributes=[],
            link_strength=0.95,
            recent_default=True,
            default_date=(datetime.now(UTC) - timedelta(days=47)).date(),
            default_amount=5000.0,
        )
        bundle = make_bundle({
            "related_parties": [dan],
            "shared_attributes": ["device_fingerprint", "address"],
            "fund_flow_signals": {
                "transferred_to_related_defaulter": True,
                "transfer_hours_after_disbursement": 22,
            },
            "graph_risk": GraphRiskOutput(
                related_parties=[dan],
                household_default_count=1,
                shortest_path_to_defaulter=1,
                fund_flow_to_defaulter=True,
                cluster_density=0.3,
                graph_risk_score=0.75,
            ),
        })
        result = self.engine.evaluate(bundle)
        codes = [h.rule_code for h in result.rule_hits]

        assert "R001_HOUSEHOLD_RECENT_DEFAULT" in codes
        assert "R008_SHARED_DEVICE_WITH_RECENT_DEFAULTER" in codes
        assert "R003_FUNDS_TRANSFERRED_TO_RELATED_DEFAULTER" in codes
        assert result.critical_hit is True
        assert result.manual_review_required is True

    def test_legitimate_independent_scenario(self):
        """
        Legitimate scenario: sibling defaulted but NO shared signals.
        Should trigger only R001 (warning), not critical.
        """
        dan = RelatedParty(
            person_id="dan-001",
            name="Dan Johnson",
            relationship_type="sibling",
            shared_attributes=[],
            link_strength=0.5,
            recent_default=True,
            default_date=(datetime.now(UTC) - timedelta(days=75)).date(),
            default_amount=5000.0,
        )
        bundle = make_bundle({
            "related_parties": [dan],
            "shared_attributes": [],
            "fund_flow_signals": {},
        })
        result = self.engine.evaluate(bundle)
        assert result.critical_hit is False
        critical_hits = [h for h in result.rule_hits if h.severity == RuleSeverity.CRITICAL]
        assert len(critical_hits) == 0

    def test_r009_rapid_disbursement(self):
        """R009: Full amount transferred out within 24h."""
        bundle = make_bundle({
            "fund_flow_signals": {
                "rapid_full_transfer": True,
                "transfer_hours_after_disbursement": 18,
            }
        })
        result = self.engine.evaluate(bundle)
        codes = [h.rule_code for h in result.rule_hits]
        assert "R009_RAPID_FULL_DISBURSEMENT_TRANSFER" in codes

    def test_r011_income_overstatement(self):
        """R011: Declared income >2.5x bureau estimate."""
        bundle = make_bundle({
            "bureau_summary": {
                "declared_income": 150000,
                "bureau_income_estimate": 40000,
            }
        })
        result = self.engine.evaluate(bundle)
        codes = [h.rule_code for h in result.rule_hits]
        assert "R011_INCOME_OVERSTATEMENT" in codes

    def test_r007_high_household_default_velocity(self):
        """R007: Household with 3+ defaults in 12 months."""
        bundle = make_bundle({
            "graph_risk": GraphRiskOutput(
                related_parties=[],
                household_default_count=4,
                shortest_path_to_defaulter=1,
                fund_flow_to_defaulter=False,
                cluster_density=0.2,
                graph_risk_score=0.5,
            )
        })
        result = self.engine.evaluate(bundle)
        codes = [h.rule_code for h in result.rule_hits]
        assert "R007_HIGH_HOUSEHOLD_DEFAULT_VELOCITY" in codes
        assert result.critical_hit is True
