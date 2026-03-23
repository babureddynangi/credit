# rules/engine.py
# Deterministic rule engine — always explainable, auditable, fast

from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any
import logging

from api.schemas.models import (
    EvidenceBundle, RuleHit, RuleEngineOutput, RuleSeverity
)

logger = logging.getLogger(__name__)

UTC = timezone.utc


class RuleEngine:
    """
    Deterministic policy rule engine.

    All rules are hard-coded, version-controlled, and produce named
    reason codes mapped to ECOA/FCRA adverse-action categories.

    Rules fire independently; outputs are accumulated into a RuleEngineOutput.
    A 'critical' hit automatically triggers manual_review_required=True.
    """

    # Configurable thresholds (load from config/env in production)
    RELATED_PARTY_DEFAULT_WINDOW_DAYS = 90
    SHARED_ACCOUNT_WINDOW_DAYS        = 90
    FUND_TRANSFER_WINDOW_DAYS         = 7
    DEVICE_FRAUD_WINDOW_DAYS          = 180
    PROXY_FUND_TRANSFER_HOURS         = 24

    def evaluate(self, bundle: EvidenceBundle) -> RuleEngineOutput:
        hits: List[RuleHit] = []

        hits += self._r001_prior_default_in_household(bundle)
        hits += self._r002_shared_bank_account(bundle)
        hits += self._r003_funds_transferred_to_related_defaulter(bundle)
        hits += self._r004_device_tied_to_prior_fraud(bundle)
        hits += self._r005_contact_controlled_by_other_borrower(bundle)
        hits += self._r006_purpose_transaction_mismatch(bundle)
        hits += self._r007_application_velocity(bundle)
        hits += self._r008_shared_device_with_recent_defaulter(bundle)
        hits += self._r009_rapid_disbursement_transfer(bundle)
        hits += self._r010_multiple_apps_same_household(bundle)
        hits += self._r011_income_verification_mismatch(bundle)
        hits += self._r012_address_change_pre_application(bundle)

        critical_hit = any(h.severity == RuleSeverity.CRITICAL for h in hits)
        manual_review = critical_hit or len([h for h in hits if h.severity == RuleSeverity.WARNING]) >= 2

        return RuleEngineOutput(
            rule_hits=hits,
            critical_hit=critical_hit,
            manual_review_required=manual_review,
        )

    # ─── Individual Rules ────────────────────────────────────────────────────

    def _r001_prior_default_in_household(self, b: EvidenceBundle) -> List[RuleHit]:
        """R001: Household member defaulted within lookback window."""
        hits = []
        cutoff = datetime.now(UTC) - timedelta(days=self.RELATED_PARTY_DEFAULT_WINDOW_DAYS)
        for party in b.related_parties:
            if party.recent_default and party.default_date:
                default_dt = datetime.combine(party.default_date,
                                              datetime.min.time()).replace(tzinfo=UTC)
                if default_dt >= cutoff:
                    hits.append(RuleHit(
                        rule_code="R001_HOUSEHOLD_RECENT_DEFAULT",
                        description=(
                            f"Related party '{party.name}' ({party.relationship_type}) "
                            f"defaulted {party.default_date} within "
                            f"{self.RELATED_PARTY_DEFAULT_WINDOW_DAYS}-day window."
                        ),
                        severity=RuleSeverity.WARNING,
                        evidence={
                            "related_party": party.name,
                            "default_date": str(party.default_date),
                            "default_amount": party.default_amount,
                        },
                    ))
        return hits

    def _r002_shared_bank_account(self, b: EvidenceBundle) -> List[RuleHit]:
        """R002: Same bank account used by applicant and a related-party defaulter."""
        if "bank_account" in b.shared_attributes:
            defaulters = [p for p in b.related_parties if p.recent_default]
            if defaulters:
                return [RuleHit(
                    rule_code="R002_SHARED_BANK_ACCOUNT_WITH_DEFAULTER",
                    description=(
                        "Applicant shares bank account with a related party who recently defaulted."
                    ),
                    severity=RuleSeverity.CRITICAL,
                    evidence={
                        "shared_attribute": "bank_account",
                        "defaulters": [p.name for p in defaulters],
                    },
                )]
        return []

    def _r003_funds_transferred_to_related_defaulter(self, b: EvidenceBundle) -> List[RuleHit]:
        """R003: Loan disbursement transferred to known defaulter within 7 days."""
        if b.fund_flow_signals.get("transferred_to_related_defaulter"):
            hours = b.fund_flow_signals.get("transfer_hours_after_disbursement", 999)
            if hours <= self.FUND_TRANSFER_WINDOW_DAYS * 24:
                return [RuleHit(
                    rule_code="R003_FUNDS_TRANSFERRED_TO_RELATED_DEFAULTER",
                    description=(
                        f"Loan disbursement transferred to related defaulter "
                        f"within {hours:.0f} hours of disbursement."
                    ),
                    severity=RuleSeverity.CRITICAL,
                    evidence=b.fund_flow_signals,
                )]
        return []

    def _r004_device_tied_to_prior_fraud(self, b: EvidenceBundle) -> List[RuleHit]:
        """R004: Device fingerprint associated with prior confirmed fraud case."""
        if b.device_signals.get("device_in_fraud_registry"):
            return [RuleHit(
                rule_code="R004_DEVICE_IN_FRAUD_REGISTRY",
                description="Application submitted from device linked to prior fraud/default case.",
                severity=RuleSeverity.CRITICAL,
                evidence={
                    "device_fingerprint": b.device_signals.get("device_fingerprint"),
                    "prior_case_id": b.device_signals.get("prior_case_id"),
                },
            )]
        return []

    def _r005_contact_controlled_by_other_borrower(self, b: EvidenceBundle) -> List[RuleHit]:
        """R005: Phone number is primary contact of another active/defaulted borrower."""
        if b.device_signals.get("phone_controlled_by_other_borrower"):
            return [RuleHit(
                rule_code="R005_PHONE_CONTROLLED_BY_OTHER_BORROWER",
                description="Contact phone number is registered to / controlled by another borrower.",
                severity=RuleSeverity.CRITICAL,
                evidence={"phone_owner": b.device_signals.get("phone_owner_id")},
            )]
        return []

    def _r006_purpose_transaction_mismatch(self, b: EvidenceBundle) -> List[RuleHit]:
        """R006: Declared loan purpose does not match observed transaction usage."""
        if b.fund_flow_signals.get("purpose_mismatch"):
            return [RuleHit(
                rule_code="R006_PURPOSE_TRANSACTION_MISMATCH",
                description="Declared loan purpose does not match observed post-disbursement transactions.",
                severity=RuleSeverity.WARNING,
                evidence={
                    "declared_purpose": b.fund_flow_signals.get("declared_purpose"),
                    "observed_categories": b.fund_flow_signals.get("observed_categories"),
                },
            )]
        return []

    def _r007_application_velocity(self, b: EvidenceBundle) -> List[RuleHit]:
        """R007: Multiple applications from household in short window."""
        count = b.graph_risk.household_default_count
        if count >= 3:
            return [RuleHit(
                rule_code="R007_HIGH_HOUSEHOLD_DEFAULT_VELOCITY",
                description=f"Household has {count} defaults — elevated coordinated risk signal.",
                severity=RuleSeverity.CRITICAL,
                evidence={"household_default_count": count},
            )]
        return []

    def _r008_shared_device_with_recent_defaulter(self, b: EvidenceBundle) -> List[RuleHit]:
        """R008: Shares device fingerprint with a recent defaulter."""
        if "device_fingerprint" in b.shared_attributes:
            defaulters = [p for p in b.related_parties if p.recent_default]
            if defaulters:
                return [RuleHit(
                    rule_code="R008_SHARED_DEVICE_WITH_RECENT_DEFAULTER",
                    description="Application shares device fingerprint with recent defaulter.",
                    severity=RuleSeverity.CRITICAL,
                    evidence={"defaulters": [p.name for p in defaulters]},
                )]
        return []

    def _r009_rapid_disbursement_transfer(self, b: EvidenceBundle) -> List[RuleHit]:
        """R009: Full loan amount transferred out within 24 hours of disbursement."""
        if b.fund_flow_signals.get("rapid_full_transfer"):
            hours = b.fund_flow_signals.get("transfer_hours_after_disbursement", 999)
            if hours <= self.PROXY_FUND_TRANSFER_HOURS:
                return [RuleHit(
                    rule_code="R009_RAPID_FULL_DISBURSEMENT_TRANSFER",
                    description=f"Full loan amount transferred out within {hours:.1f}h of disbursement.",
                    severity=RuleSeverity.CRITICAL,
                    evidence={"hours": hours, "amount": b.loan_amount},
                )]
        return []

    def _r010_multiple_apps_same_household(self, b: EvidenceBundle) -> List[RuleHit]:
        """R010: Multiple simultaneous applications from same household."""
        count = b.graph_risk.cluster_density
        if count >= 0.7:
            return [RuleHit(
                rule_code="R010_HIGH_CLUSTER_DENSITY",
                description="Applicant is in a high-density related-party cluster (possible fraud ring).",
                severity=RuleSeverity.WARNING,
                evidence={"cluster_density": count},
            )]
        return []

    def _r011_income_verification_mismatch(self, b: EvidenceBundle) -> List[RuleHit]:
        """R011: Declared income significantly higher than bureau-derived estimate."""
        declared = b.bureau_summary.get("declared_income")
        estimated = b.bureau_summary.get("bureau_income_estimate")
        if declared and estimated and estimated > 0:
            ratio = declared / estimated
            if ratio > 2.5:
                return [RuleHit(
                    rule_code="R011_INCOME_OVERSTATEMENT",
                    description=f"Declared income is {ratio:.1f}x bureau income estimate.",
                    severity=RuleSeverity.WARNING,
                    evidence={"declared": declared, "estimated": estimated, "ratio": ratio},
                )]
        return []

    def _r012_address_change_pre_application(self, b: EvidenceBundle) -> List[RuleHit]:
        """R012: Address changed within 30 days before application (identity risk signal)."""
        if b.device_signals.get("recent_address_change_days", 9999) < 30:
            days = b.device_signals["recent_address_change_days"]
            return [RuleHit(
                rule_code="R012_RECENT_ADDRESS_CHANGE",
                description=f"Address changed {days} days before application — identity risk signal.",
                severity=RuleSeverity.WARNING,
                evidence={"days_before_application": days},
            )]
        return []
