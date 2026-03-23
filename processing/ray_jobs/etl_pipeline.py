# processing/ray_jobs/etl_pipeline.py
# Ray Data ETL pipeline — distributed processing for all application data
# Uses Ray Data for batch/streaming, Ray Serve exposes online inference endpoints

from __future__ import annotations
import os
import re
import hashlib
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

import ray
import ray.data
from ray import serve

logger = logging.getLogger(__name__)
UTC = timezone.utc


# ─── Ray Cluster Init ─────────────────────────────────────────────────────────

def init_ray():
    """Initialize Ray — connects to cluster in prod, local in dev."""
    address = os.getenv("RAY_ADDRESS", "auto")
    if not ray.is_initialized():
        if address == "auto":
            try:
                ray.init(address="auto", ignore_reinit_error=True)
            except Exception:
                ray.init(ignore_reinit_error=True)  # local fallback
        else:
            ray.init(address=address, ignore_reinit_error=True)
    logger.info(f"Ray initialized: {ray.cluster_resources()}")


# ─── Application ETL ─────────────────────────────────────────────────────────

class ApplicationNormalizer:
    """
    Ray Data batch processor.
    Applied to raw application payloads from Kafka.
    Produces standardized records ready for entity resolution.
    """

    @staticmethod
    def normalize_batch(batch: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize a batch of application records.
        Called by ray.data.Dataset.map_batches().
        """
        import pandas as pd

        df = pd.DataFrame(batch)

        # Name normalization
        df["full_name_normalized"] = (
            df["full_name"]
            .str.upper()
            .str.strip()
            .str.replace(r"\s+", " ", regex=True)
        )

        # Phone E.164 normalization
        df["phone_normalized"] = df["phone"].apply(
            ApplicationNormalizer._normalize_phone
        )

        # Email lowercase
        df["email_normalized"] = df["email"].str.lower().str.strip()

        # Address normalization (basic; use USPS API for production)
        df["address_normalized"] = (
            df["address"]
            .str.upper()
            .str.strip()
            .str.replace(r"\s+", " ", regex=True)
            .str.replace("STREET", "ST")
            .str.replace("AVENUE", "AVE")
            .str.replace("BOULEVARD", "BLVD")
            .str.replace("APARTMENT", "APT")
        )

        # SSN hash (never store plain SSN)
        df["ssn_hash"] = df["ssn_last4"].apply(
            lambda x: hashlib.sha256(str(x).encode()).hexdigest() if x else None
        )

        # Amount bucketing for feature engineering
        df["loan_amount_bucket"] = pd.cut(
            df["loan_amount"],
            bins=[0, 1000, 5000, 10000, 25000, 50000, float("inf")],
            labels=["micro", "small", "medium", "large", "xl", "jumbo"]
        ).astype(str)

        # Days since application submitted
        df["submitted_at"] = pd.to_datetime(df["submitted_at"], utc=True)
        df["days_since_submission"] = (
            pd.Timestamp.now(tz="UTC") - df["submitted_at"]
        ).dt.days

        return df.to_dict(orient="list")

    @staticmethod
    def _normalize_phone(phone: Optional[str]) -> Optional[str]:
        if not phone:
            return None
        digits = re.sub(r"\D", "", phone)
        if len(digits) == 10:
            return f"+1{digits}"
        elif len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"
        return digits or None


@ray.remote
class EntityResolutionActor:
    """
    Ray Actor for entity resolution.
    Runs in parallel — each actor handles a partition of applicants.
    Resolves: same person, household members, hidden related parties.
    """

    def __init__(self, db_url: str):
        import psycopg2
        self.conn = psycopg2.connect(db_url)
        self.cursor = self.conn.cursor()

    def resolve(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """
        Match incoming record against existing persons using fuzzy matching.
        Returns enriched record with person_id, household_id, and related_party_ids.
        """
        candidates = self._find_candidates(record)
        best_match  = self._score_candidates(record, candidates)

        if best_match and best_match["confidence"] >= 0.85:
            person_id = best_match["person_id"]
            household_id = best_match["household_id"]
        else:
            # New person — will be inserted by downstream service
            person_id    = None
            household_id = None

        related_ids = self._find_related_parties(
            record, person_id, household_id
        )

        return {
            **record,
            "resolved_person_id":    person_id,
            "resolved_household_id": household_id,
            "related_party_ids":     related_ids,
            "identity_confidence":   best_match["confidence"] if best_match else 0.0,
        }

    def _find_candidates(self, record: Dict) -> List[Dict]:
        """Query DB for potential matches on key identity fields."""
        query = """
            SELECT p.person_id, p.household_id,
                   p.full_name, p.ssn_hash, p.dob,
                   p.identity_confidence
            FROM persons p
            WHERE p.ssn_hash = %(ssn_hash)s
               OR (p.full_name ILIKE %(name_pattern)s AND p.dob = %(dob)s)
            LIMIT 10
        """
        self.cursor.execute(query, {
            "ssn_hash":    record.get("ssn_hash"),
            "name_pattern": f"%{record.get('full_name_normalized', '')}%",
            "dob":         record.get("dob"),
        })
        return [dict(zip([d[0] for d in self.cursor.description], row))
                for row in self.cursor.fetchall()]

    def _score_candidates(self, record: Dict, candidates: List[Dict]) -> Optional[Dict]:
        """Simple scoring: SSN match = high confidence, name+dob = medium."""
        best = None
        for c in candidates:
            score = 0.0
            if c.get("ssn_hash") and c["ssn_hash"] == record.get("ssn_hash"):
                score += 0.70
            if c.get("full_name", "").upper() == record.get("full_name_normalized", ""):
                score += 0.15
            if str(c.get("dob")) == str(record.get("dob")):
                score += 0.15
            c["confidence"] = min(score, 1.0)
            if not best or c["confidence"] > best["confidence"]:
                best = c
        return best

    def _find_related_parties(
        self, record: Dict, person_id: Optional[str], household_id: Optional[str]
    ) -> List[str]:
        """Find persons sharing key attributes with this applicant."""
        related = []

        if household_id:
            self.cursor.execute(
                "SELECT person_id FROM persons WHERE household_id = %s AND person_id != %s",
                (household_id, person_id or "")
            )
            related += [row[0] for row in self.cursor.fetchall()]

        # Shared phone
        if record.get("phone_normalized"):
            self.cursor.execute("""
                SELECT DISTINCT p.person_id FROM persons p
                JOIN applications a ON a.person_id = p.person_id
                WHERE a.phone_normalized = %s AND p.person_id != %s
            """, (record["phone_normalized"], person_id or ""))
            related += [row[0] for row in self.cursor.fetchall()]

        return list(set(related))

    def close(self):
        self.cursor.close()
        self.conn.close()


# ─── Ray Data Pipeline ────────────────────────────────────────────────────────

def run_application_etl_pipeline(
    raw_records: List[Dict[str, Any]],
    db_url: str,
) -> List[Dict[str, Any]]:
    """
    Full ETL pipeline for a batch of application records.
    1. Normalize
    2. Entity resolve
    3. Return enriched records
    """
    init_ray()

    # Step 1: Normalize via Ray Data
    ds = ray.data.from_items(raw_records)
    normalized_ds = ds.map_batches(
        ApplicationNormalizer.normalize_batch,
        batch_format="pandas",
        batch_size=256,
        num_cpus=1,
    )

    # Step 2: Entity resolution via Ray Actor pool
    num_actors = min(8, max(1, len(raw_records) // 100))
    resolver_pool = [
        EntityResolutionActor.remote(db_url)
        for _ in range(num_actors)
    ]

    normalized_list = normalized_ds.take_all()
    futures = []
    for i, record in enumerate(normalized_list):
        actor = resolver_pool[i % num_actors]
        futures.append(actor.resolve.remote(record))

    enriched = ray.get(futures)

    # Cleanup actors
    for actor in resolver_pool:
        actor.close.remote()

    return enriched


# ─── Batch Scoring Job (nightly) ─────────────────────────────────────────────

@ray.remote
def batch_score_applications(application_ids: List[str], db_url: str) -> Dict:
    """
    Nightly batch job: re-score all open applications.
    Useful for catching cases that become riskier over time.
    """
    import psycopg2
    conn = psycopg2.connect(db_url)
    cur  = conn.cursor()

    # Fetch applications
    cur.execute("""
        SELECT application_id, person_id, loan_amount, bureau_score
        FROM applications
        WHERE application_id = ANY(%s)
          AND operational_status IN ('manual_review', 'hold', 'pending')
    """, (application_ids,))

    results = []
    for row in cur.fetchall():
        app_id, person_id, amount, bureau = row
        # Placeholder: real scoring would call ML model endpoints
        results.append({
            "application_id": app_id,
            "rescored_at": datetime.now(UTC).isoformat(),
            "status": "rescored",
        })

    cur.close()
    conn.close()
    return {"rescored": len(results), "results": results}
