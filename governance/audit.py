# governance/audit.py
# Immutable audit trail — NIST AI RMF + FCRA + ECOA compliance
# Every model decision, LLM call, and human action is logged

from __future__ import annotations
import os
import uuid
import json
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import psycopg2
import psycopg2.extras
import boto3

from api.schemas.models import EvidenceBundle, LLMCaseClassification

logger = logging.getLogger(__name__)
UTC = timezone.utc


class AuditLogger:
    """
    Writes to two destinations:
    1. PostgreSQL audit_trail table (immutable via DB rules)
    2. CloudWatch Logs (long-term retention, 7 years for FCRA)
    """

    def __init__(self):
        self.db_url      = os.environ.get("DATABASE_URL")
        self.cw_group    = os.getenv("AUDIT_LOG_GROUP", "/credit-fraud/audit")
        self.llm_group   = os.getenv("LLM_LOG_GROUP",   "/credit-fraud/llm-audit")
        self._cw_client  = None

    def _get_cw(self):
        if not self._cw_client:
            self._cw_client = boto3.client(
                "logs",
                region_name=os.getenv("AWS_REGION", "us-east-1"),
            )
        return self._cw_client

    # ─── Decision Audit ───────────────────────────────────────────────────────

    def log_decision(
        self,
        case_id: str,
        application_id: str,
        bundle: EvidenceBundle,
        classification: LLMCaseClassification,
        policy_output: Dict[str, Any],
        llm_log: Dict[str, Any],
        latency_ms: int,
    ):
        """Log a complete automated decision to audit trail."""
        audit_record = {
            "audit_id":      str(uuid.uuid4()),
            "entity_type":   "case",
            "entity_id":     case_id,
            "action":        "automated_decision",
            "actor_id":      f"system:llm_classification_agent",
            "actor_type":    "llm",
            "after_state": {
                "case_type":            classification.case_type,
                "confidence":           classification.confidence,
                "recommended_action":   classification.recommended_action,
                "fraud_label":          classification.fraud_label,
                "human_review":         classification.human_review_required,
                "adverse_action_codes": classification.adverse_action_codes,
                "operational_status":   str(policy_output.get("operational_status")),
                "priority":             policy_output.get("priority"),
                "scores": {
                    "pd":    bundle.scores.pd_score,
                    "fraud": bundle.scores.fraud_score,
                    "proxy": bundle.scores.proxy_borrower_score,
                },
                "rule_hits": [h.rule_code for h in bundle.rule_hits],
            },
            "rationale":     classification.analyst_summary,
            "created_at":    datetime.now(UTC).isoformat(),
        }

        self._write_to_db(audit_record)
        self._write_llm_log(case_id, llm_log)
        self._write_to_cloudwatch(self.cw_group, audit_record)

        # Separately log model scores for drift monitoring
        self._log_model_scores(application_id, bundle.scores, llm_log.get("model", ""))

    def log_analyst_decision(
        self,
        case_id: str,
        analyst_id: str,
        disposition: str,
        reason_codes: List[str],
        notes: Optional[str],
        override_llm: bool,
    ):
        """Log a human analyst's decision."""
        record = {
            "audit_id":    str(uuid.uuid4()),
            "entity_type": "case",
            "entity_id":   case_id,
            "action":      "analyst_disposition",
            "actor_id":    analyst_id,
            "actor_type":  "human",
            "after_state": {
                "disposition":    disposition,
                "reason_codes":   reason_codes,
                "override_llm":   override_llm,
                "notes":          notes,
            },
            "rationale":   f"Analyst {analyst_id} disposition: {disposition}. "
                           f"Reason codes: {', '.join(reason_codes)}."
                           + (f" Note: {notes}" if notes else "")
                           + (" [LLM OVERRIDE]" if override_llm else ""),
            "created_at":  datetime.now(UTC).isoformat(),
        }

        self._write_to_db(record)
        self._write_to_cloudwatch(self.cw_group, record)

    # ─── Internal Write Methods ───────────────────────────────────────────────

    def _write_to_db(self, record: Dict[str, Any]):
        try:
            conn = psycopg2.connect(self.db_url)
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO audit_trail
                        (audit_id, entity_type, entity_id, action,
                         actor_id, actor_type, after_state, rationale, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    record["audit_id"],
                    record["entity_type"],
                    record["entity_id"],
                    record["action"],
                    record["actor_id"],
                    record["actor_type"],
                    json.dumps(record.get("after_state", {})),
                    record.get("rationale"),
                    record["created_at"],
                ))
            conn.commit()
        except Exception as e:
            logger.error(f"Audit DB write failed: {e}")
        finally:
            if conn:
                conn.close()

    def _write_llm_log(self, case_id: str, llm_log: Dict[str, Any]):
        try:
            conn = psycopg2.connect(self.db_url)
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO llm_logs
                        (log_id, case_id, agent_name, model_name,
                         prompt_hash, input_tokens, output_tokens,
                         structured_output, latency_ms)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    str(uuid.uuid4()),
                    case_id,
                    "LLMClassificationAgent",
                    llm_log.get("model", ""),
                    llm_log.get("prompt_hash", ""),
                    llm_log.get("input_tokens"),
                    llm_log.get("output_tokens"),
                    json.dumps(llm_log.get("structured_output", {})),
                    llm_log.get("latency_ms"),
                ))
            conn.commit()
        except Exception as e:
            logger.error(f"LLM log DB write failed: {e}")
        finally:
            if conn:
                conn.close()

    def _log_model_scores(self, application_id: str, scores, model_version: str):
        try:
            conn = psycopg2.connect(self.db_url)
            with conn.cursor() as cur:
                for model_name, score_val in [
                    ("pd_model", scores.pd_score),
                    ("fraud_model", scores.fraud_score),
                    ("proxy_borrower_model", scores.proxy_borrower_score),
                ]:
                    cur.execute("""
                        INSERT INTO model_scores
                            (score_id, application_id, model_name, model_version, score_value)
                        VALUES (%s,%s,%s,%s,%s)
                    """, (str(uuid.uuid4()), application_id, model_name, model_version, score_val))
            conn.commit()
        except Exception as e:
            logger.error(f"Model score log failed: {e}")
        finally:
            if conn:
                conn.close()

    def _write_to_cloudwatch(self, log_group: str, record: Dict[str, Any]):
        """Send to CloudWatch for long-term FCRA-compliant retention (7 years)."""
        if not os.getenv("AWS_REGION"):
            return  # Skip in local dev
        try:
            cw = self._get_cw()
            stream = f"audit/{datetime.now(UTC).strftime('%Y/%m/%d')}"
            try:
                cw.create_log_stream(logGroupName=log_group, logStreamName=stream)
            except cw.exceptions.ResourceAlreadyExistsException:
                pass
            cw.put_log_events(
                logGroupName=log_group,
                logStreamName=stream,
                logEvents=[{
                    "timestamp": int(datetime.now(UTC).timestamp() * 1000),
                    "message": json.dumps(record),
                }]
            )
        except Exception as e:
            logger.warning(f"CloudWatch audit log failed (non-fatal): {e}")


# ─── Monitoring / Drift Detection ────────────────────────────────────────────

class ModelMonitor:
    """
    Tracks model score distributions for drift detection.
    NIST AI RMF: Measure and Manage functions.
    Compliant with UDAAP: monitors for potential disparate impact.
    """

    # Alert thresholds
    FRAUD_FPR_THRESHOLD  = 0.10   # Alert if false positive rate > 10%
    PD_PSI_THRESHOLD     = 0.20   # Alert if Population Stability Index > 0.20
    PROXY_TPR_THRESHOLD  = 0.70   # Alert if true positive rate < 70%

    def compute_psi(self, expected: List[float], actual: List[float], bins: int = 10) -> float:
        """Population Stability Index — detects score distribution drift."""
        import numpy as np
        expected_arr = np.array(expected)
        actual_arr   = np.array(actual)

        breakpoints = np.linspace(0, 1, bins + 1)
        exp_pct = np.histogram(expected_arr, bins=breakpoints)[0] / len(expected_arr)
        act_pct = np.histogram(actual_arr,   bins=breakpoints)[0] / len(actual_arr)

        # Avoid division by zero
        exp_pct = np.where(exp_pct == 0, 0.0001, exp_pct)
        act_pct = np.where(act_pct == 0, 0.0001, act_pct)

        psi = np.sum((act_pct - exp_pct) * np.log(act_pct / exp_pct))
        return float(psi)

    def check_disparate_impact(
        self,
        approvals_group_a: int, total_group_a: int,
        approvals_group_b: int, total_group_b: int,
    ) -> Dict[str, Any]:
        """
        ECOA / UDAAP: 4/5ths rule check.
        If approval rate of protected class < 80% of majority class, flag for review.
        NOTE: Protected class info must NEVER flow into models — this is a post-hoc check.
        """
        if total_group_a == 0 or total_group_b == 0:
            return {"di_ratio": None, "alert": False}

        rate_a = approvals_group_a / total_group_a
        rate_b = approvals_group_b / total_group_b

        di_ratio = min(rate_a, rate_b) / max(rate_a, rate_b) if max(rate_a, rate_b) > 0 else 1.0

        return {
            "di_ratio": round(di_ratio, 4),
            "alert":    di_ratio < 0.80,   # ECOA 4/5 rule
            "rate_a":   round(rate_a, 4),
            "rate_b":   round(rate_b, 4),
        }
