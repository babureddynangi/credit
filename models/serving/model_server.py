# models/serving/model_server.py
# Ray Serve deployment for all three scoring models
# PD model, Fraud model, Proxy Borrower model

from __future__ import annotations
import os
import logging
import numpy as np
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone

from ray import serve
from fastapi import FastAPI

from api.schemas.models import ModelScores

logger = logging.getLogger(__name__)
UTC    = timezone.utc

# FastAPI app for Ray Serve
app = FastAPI(title="Credit Fraud Model Serving API")


# ─── Feature Extraction ───────────────────────────────────────────────────────

def extract_features(evidence: Dict[str, Any]) -> np.ndarray:
    """
    Extract a feature vector from the evidence bundle.
    In production: load from Feature Store (DynamoDB) via feature pipeline.
    """
    bureau_score     = float(evidence.get("bureau_score") or 650) / 850.0
    loan_amount      = np.log1p(float(evidence.get("loan_amount") or 0)) / 15.0
    declared_income  = np.log1p(float(evidence.get("declared_income") or 1)) / 15.0
    dti_ratio        = float(evidence.get("loan_amount") or 0) / max(float(evidence.get("declared_income") or 1), 1)
    prior_defaults   = float(evidence.get("prior_defaults") or 0) / 5.0
    household_defaults = float(evidence.get("graph", {}).get("household_default_count") or 0) / 5.0
    shared_attrs     = len(evidence.get("shared_attributes") or []) / 6.0
    cluster_density  = float(evidence.get("graph", {}).get("cluster_density") or 0)
    shortest_path    = 1.0 / max(float(evidence.get("graph", {}).get("shortest_path_to_defaulter") or 5), 1)
    fund_flow        = float(bool(evidence.get("graph", {}).get("fund_flow_to_defaulter", False)))
    device_fraud     = float(bool(evidence.get("device_signals", {}).get("device_in_fraud_registry", False)))
    recent_addr_chg  = float(evidence.get("device_signals", {}).get("recent_address_change_days", 365) < 30)
    rule_critical    = float(any(
        h.get("severity") == "critical"
        for h in evidence.get("rule_hits") or []
    ))
    rule_warning_ct  = min(sum(
        1 for h in (evidence.get("rule_hits") or [])
        if h.get("severity") == "warning"
    ) / 5.0, 1.0)

    return np.array([
        bureau_score, loan_amount, declared_income, dti_ratio,
        prior_defaults, household_defaults, shared_attrs,
        cluster_density, shortest_path, fund_flow,
        device_fraud, recent_addr_chg, rule_critical, rule_warning_ct,
    ], dtype=np.float32)


# ─── Model Deployments ────────────────────────────────────────────────────────

@serve.deployment(
    num_replicas=2,
    ray_actor_options={"num_cpus": 1},
    autoscaling_config={"min_replicas": 1, "max_replicas": 10},
)
@serve.ingress(app)
class PDModelDeployment:
    """
    Probability of Default (PD) model.
    Predicts ordinary non-payment risk.
    In production: loads XGBoost/LightGBM from S3 model registry.
    """

    def __init__(self):
        self.model_version = os.getenv("PD_MODEL_VERSION", "v1.2.0")
        self.model = self._load_model()
        logger.info(f"PD model loaded: {self.model_version}")

    def _load_model(self):
        """Load model from S3 / SageMaker endpoint in production."""
        model_path = os.getenv("PD_MODEL_PATH")
        if model_path and os.path.exists(model_path):
            try:
                import joblib
                return joblib.load(model_path)
            except Exception as e:
                logger.warning(f"Could not load PD model from {model_path}: {e}")
        # Fallback: linear scoring heuristic for local dev
        return None

    @app.post("/score/pd")
    def score(self, evidence: Dict[str, Any]) -> Dict[str, Any]:
        features = extract_features(evidence)
        if self.model:
            score = float(self.model.predict_proba([features])[0][1])
        else:
            # Heuristic fallback
            score = self._heuristic_pd(evidence, features)
        return {
            "model": "pd_model",
            "version": self.model_version,
            "score": round(score, 4),
            "scored_at": datetime.now(UTC).isoformat(),
        }

    def _heuristic_pd(self, evidence: Dict, features: np.ndarray) -> float:
        """Simple rule-based PD heuristic for dev/testing."""
        bureau = evidence.get("bureau_score") or 650
        if bureau >= 720:
            base = 0.05
        elif bureau >= 680:
            base = 0.15
        elif bureau >= 620:
            base = 0.30
        else:
            base = 0.50
        base += features[4] * 0.20   # prior defaults
        base += features[5] * 0.10   # household defaults
        return min(base, 0.99)


@serve.deployment(
    num_replicas=2,
    ray_actor_options={"num_cpus": 1},
    autoscaling_config={"min_replicas": 1, "max_replicas": 10},
)
@serve.ingress(app)
class FraudModelDeployment:
    """
    Application Fraud model.
    Predicts deception/misrepresentation risk at origination.
    """

    def __init__(self):
        self.model_version = os.getenv("FRAUD_MODEL_VERSION", "v2.1.0")
        self.model = self._load_model()

    def _load_model(self):
        model_path = os.getenv("FRAUD_MODEL_PATH")
        if model_path and os.path.exists(model_path):
            try:
                import joblib
                return joblib.load(model_path)
            except Exception:
                pass
        return None

    @app.post("/score/fraud")
    def score(self, evidence: Dict[str, Any]) -> Dict[str, Any]:
        features = extract_features(evidence)
        if self.model:
            score = float(self.model.predict_proba([features])[0][1])
        else:
            score = self._heuristic_fraud(evidence, features)
        return {
            "model": "fraud_model",
            "version": self.model_version,
            "score": round(score, 4),
            "scored_at": datetime.now(UTC).isoformat(),
        }

    def _heuristic_fraud(self, evidence: Dict, features: np.ndarray) -> float:
        score = 0.0
        score += features[9]  * 0.35   # fund flow to defaulter
        score += features[10] * 0.25   # device in fraud registry
        score += features[11] * 0.15   # recent address change
        score += features[12] * 0.20   # critical rule hit
        score += features[13] * 0.10   # warning rule count
        return min(round(score, 4), 0.99)


@serve.deployment(
    num_replicas=2,
    ray_actor_options={"num_cpus": 1},
    autoscaling_config={"min_replicas": 1, "max_replicas": 10},
)
@serve.ingress(app)
class ProxyBorrowerModelDeployment:
    """
    Proxy Borrower / Related-Party Misuse model.
    Predicts whether named borrower is NOT the real beneficiary.
    This is the key differentiator from standard PD/fraud models.
    """

    def __init__(self):
        self.model_version = os.getenv("PROXY_MODEL_VERSION", "v1.0.0")
        self.model = self._load_model()

    def _load_model(self):
        model_path = os.getenv("PROXY_MODEL_PATH")
        if model_path and os.path.exists(model_path):
            try:
                import joblib
                return joblib.load(model_path)
            except Exception:
                pass
        return None

    @app.post("/score/proxy")
    def score(self, evidence: Dict[str, Any]) -> Dict[str, Any]:
        features = extract_features(evidence)
        if self.model:
            score = float(self.model.predict_proba([features])[0][1])
        else:
            score = self._heuristic_proxy(evidence, features)
        return {
            "model": "proxy_borrower_model",
            "version": self.model_version,
            "score": round(score, 4),
            "scored_at": datetime.now(UTC).isoformat(),
        }

    def _heuristic_proxy(self, evidence: Dict, features: np.ndarray) -> float:
        """
        Proxy borrower heuristic:
        High weight on fund flow, shared attributes, and related-party defaults.
        """
        score = 0.0
        score += features[9]  * 0.40   # fund flow to defaulter — strongest signal
        score += features[6]  * 0.25   # shared attributes (device, account, address)
        score += features[5]  * 0.20   # household default count
        score += features[8]  * 0.15   # shortest path to defaulter
        return min(round(score, 4), 0.99)


# ─── Scoring Orchestrator ─────────────────────────────────────────────────────

class ScoringOrchestrator:
    """
    Calls all three model endpoints and returns a combined ModelScores object.
    Used by the Decision Orchestrator in the API layer.
    """

    def __init__(self):
        self.pd_handle      = serve.get_deployment_handle("PDModelDeployment")
        self.fraud_handle   = serve.get_deployment_handle("FraudModelDeployment")
        self.proxy_handle   = serve.get_deployment_handle("ProxyBorrowerModelDeployment")

    async def score_all(self, evidence: Dict[str, Any]) -> ModelScores:
        import asyncio
        pd_res, fraud_res, proxy_res = await asyncio.gather(
            self.pd_handle.score.remote(evidence),
            self.fraud_handle.score.remote(evidence),
            self.proxy_handle.score.remote(evidence),
        )
        return ModelScores(
            pd_score=pd_res["score"],
            fraud_score=fraud_res["score"],
            proxy_borrower_score=proxy_res["score"],
            model_version=f"pd:{pd_res['version']},fraud:{fraud_res['version']},proxy:{proxy_res['version']}",
        )


# ─── Serve Application Entry Point ───────────────────────────────────────────

def deploy_models():
    """Deploy all model endpoints to Ray Serve."""
    serve.start(detached=True, http_options={"host": "0.0.0.0", "port": 8100})
    PDModelDeployment.bind()
    FraudModelDeployment.bind()
    ProxyBorrowerModelDeployment.bind()
    logger.info("All three scoring models deployed to Ray Serve on port 8100")


if __name__ == "__main__":
    deploy_models()
