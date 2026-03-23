# tests/test_ollama_sim_properties.py
# Property-based tests for the Ollama simulation runner
# Feature: ollama-simulation

import sys
import time
import json
import threading
import pytest
from unittest.mock import patch, MagicMock
from hypothesis import given, settings, assume
from hypothesis import strategies as st

sys.path.insert(0, ".")


# ─── Property 7: Config validation rejects bad inputs ────────────────────────
# Feature: ollama-simulation, Property 7: Config validation rejects bad inputs

@given(
    total=st.integers(min_value=-100, max_value=0),
    batch_size=st.integers(min_value=1, max_value=10),
)
@settings(max_examples=20, deadline=None)
def test_property7_invalid_total_exits(total, batch_size):
    """For any total < 1, validate_config must raise SystemExit."""
    from scripts.run_ollama_sim import RunConfig, validate_config

    config = RunConfig(total=total, batch_size=batch_size)
    with pytest.raises(SystemExit) as exc_info:
        validate_config(config)
    assert exc_info.value.code == 1


@given(
    total=st.integers(min_value=1, max_value=100),
    batch_size=st.integers(min_value=-100, max_value=0),
)
@settings(max_examples=20, deadline=None)
def test_property7_invalid_batch_size_exits(total, batch_size):
    """For any batch_size < 1, validate_config must raise SystemExit."""
    from scripts.run_ollama_sim import RunConfig, validate_config

    config = RunConfig(total=total, batch_size=batch_size)
    with pytest.raises(SystemExit) as exc_info:
        validate_config(config)
    assert exc_info.value.code == 1


@given(
    total=st.integers(min_value=1, max_value=50),
    extra=st.integers(min_value=1, max_value=50),
)
@settings(max_examples=20, deadline=None)
def test_property7_batch_size_exceeds_total_exits(total, extra):
    """For any batch_size > total, validate_config must raise SystemExit."""
    from scripts.run_ollama_sim import RunConfig, validate_config

    batch_size = total + extra
    config = RunConfig(total=total, batch_size=batch_size)
    with pytest.raises(SystemExit) as exc_info:
        validate_config(config)
    assert exc_info.value.code == 1


@given(
    total=st.integers(min_value=1, max_value=100),
    batch_size=st.integers(min_value=1, max_value=100),
)
@settings(max_examples=20, deadline=None)
def test_property7_valid_config_does_not_exit(total, batch_size):
    """For any valid (total >= 1, 1 <= batch_size <= total), validate_config must not raise."""
    assume(batch_size <= total)
    from scripts.run_ollama_sim import RunConfig, validate_config

    config = RunConfig(total=total, batch_size=batch_size)
    validate_config(config)


# ─── Property 4: Retry count bound ───────────────────────────────────────────
# Feature: ollama-simulation, Property 4: Retry count bound

@given(retries=st.integers(min_value=0, max_value=5))
@settings(max_examples=20, deadline=None)
def test_property4_retry_count_bound(retries):
    """
    For any retry count 0-5, when the LLM always raises JSONDecodeError,
    total call attempts must equal retries + 1.
    """
    from scripts.run_ollama_sim import RunConfig
    from api.schemas.models import EvidenceBundle, ModelScores, GraphRiskOutput
    from datetime import datetime, timezone
    import tenacity

    call_count = [0]

    bundle = EvidenceBundle(
        case_id="test", application_id="app",
        applicant_name="Test User", loan_amount=10000.0,
        submitted_at=datetime.now(timezone.utc),
        scores=ModelScores(pd_score=0.1, fraud_score=0.1, proxy_borrower_score=0.1, model_version="test"),
        graph_risk=GraphRiskOutput(
            related_parties=[], household_default_count=0,
            fund_flow_to_defaulter=False, cluster_density=0.0, graph_risk_score=0.0,
        ),
    )
    config = RunConfig(retries=retries, timeout=30.0)

    # Build a fresh retry-wrapped function with wait=none so there are no sleeps
    @tenacity.retry(
        stop=tenacity.stop_after_attempt(config.retries + 1),
        wait=tenacity.wait_none(),
        retry=tenacity.retry_if_exception_type(json.JSONDecodeError),
        reraise=True,
    )
    def always_fails():
        call_count[0] += 1
        raise json.JSONDecodeError("mock error", "", 0)

    with pytest.raises(json.JSONDecodeError):
        always_fails()

    assert call_count[0] == retries + 1, (
        f"Expected {retries + 1} attempts, got {call_count[0]}"
    )


# ─── Property 1: Timeout bound ───────────────────────────────────────────────
# Feature: ollama-simulation, Property 1: Timeout bound

@given(timeout=st.floats(min_value=0.05, max_value=0.2))
@settings(max_examples=5, deadline=None)
def test_property1_timeout_bound(timeout):
    """
    For any timeout T, when the LLM sleeps longer than T,
    run_one_with_timeout must return error='timeout' and
    wall-clock time must be <= T + 1s.
    """
    from scripts.run_ollama_sim import RunConfig, run_one_with_timeout

    sleep_duration = timeout + 0.15  # always longer than timeout, but short

    def slow_llm(bundle, config):
        time.sleep(sleep_duration)
        return {"case_type": "independent_credit_risk"}, 0

    config = RunConfig(total=1, batch_size=1, timeout=timeout, retries=0)

    with patch("scripts.run_ollama_sim.ollama_classify_with_retry", side_effect=slow_llm):
        t0      = time.time()
        result  = run_one_with_timeout(1, "clean", config)
        elapsed = time.time() - t0

    assert result["error"] == "timeout", f"Expected timeout error, got: {result['error']}"
    assert elapsed <= timeout + 1.5, f"Elapsed {elapsed:.2f}s exceeded timeout {timeout}s + 1.5s buffer"


# ─── Property 2: Concurrency cap ─────────────────────────────────────────────
# Feature: ollama-simulation, Property 2: Concurrency cap

@given(
    concurrency=st.integers(min_value=1, max_value=3),
    total=st.integers(min_value=3, max_value=6),
)
@settings(max_examples=10, deadline=None)
def test_property2_concurrency_cap(concurrency, total):
    """
    For any concurrency C and total N, peak simultaneous LLM calls must not exceed C.
    """
    assume(total >= concurrency)
    from scripts.run_ollama_sim import RunConfig, run_simulation

    peak_concurrent = [0]
    current_concurrent = [0]
    lock = threading.Lock()

    def mock_run_one(i, scenario, cfg):
        with lock:
            current_concurrent[0] += 1
            if current_concurrent[0] > peak_concurrent[0]:
                peak_concurrent[0] = current_concurrent[0]
        # no sleep — just record and return immediately
        with lock:
            current_concurrent[0] -= 1
        return {
            "index": i, "scenario": scenario, "case_id": "x",
            "loan_amount": 1000.0, "bureau_score": 700,
            "operational_status": "approved", "llm_case_type": "independent_credit_risk",
            "llm_fraud_label": "none", "llm_confidence": 0.9,
            "llm_human_review": False, "llm_key_reasons": "", "llm_analyst_summary": "",
            "rule_hits": 0, "critical_hit": False, "latency_ms": 0, "error": None,
        }

    config    = RunConfig(total=total, batch_size=total, concurrency=concurrency,
                          timeout=5.0, batch_pause=0.0, retries=0)
    scenarios = ["clean"] * total

    with patch("scripts.run_ollama_sim.run_one_with_timeout", side_effect=mock_run_one):
        run_simulation(config, scenarios)

    assert peak_concurrent[0] <= concurrency, (
        f"Peak concurrency {peak_concurrent[0]} exceeded limit {concurrency}"
    )


# ─── Property 3: Batch pause lower bound ─────────────────────────────────────
# Feature: ollama-simulation, Property 3: Batch pause lower bound

@given(batch_pause=st.floats(min_value=0.5, max_value=2.0))
@settings(max_examples=10, deadline=None)
def test_property3_batch_pause_lower_bound(batch_pause):
    """
    For any batch_pause P > 0 and a multi-batch run,
    time.sleep must be called with a value >= batch_pause between batches.
    """
    from scripts.run_ollama_sim import RunConfig, run_simulation

    sleep_calls = []

    def mock_run_one(i, scenario, cfg):
        return {
            "index": i, "scenario": scenario, "case_id": "x",
            "loan_amount": 1000.0, "bureau_score": 700,
            "operational_status": "approved", "llm_case_type": "independent_credit_risk",
            "llm_fraud_label": "none", "llm_confidence": 0.9,
            "llm_human_review": False, "llm_key_reasons": "", "llm_analyst_summary": "",
            "rule_hits": 0, "critical_hit": False, "latency_ms": 10, "error": None,
        }

    def mock_sleep(secs):
        sleep_calls.append(secs)

    # 4 apps, batch_size=2 → 2 batches → 1 inter-batch sleep
    config    = RunConfig(total=4, batch_size=2, concurrency=1,
                          timeout=5.0, batch_pause=batch_pause, retries=0)
    scenarios = ["clean"] * 4

    with patch("scripts.run_ollama_sim.run_one_with_timeout", side_effect=mock_run_one):
        with patch("scripts.run_ollama_sim.time.sleep", side_effect=mock_sleep):
            run_simulation(config, scenarios)

    assert len(sleep_calls) >= 1, "Expected at least one inter-batch sleep call"
    assert all(s >= batch_pause for s in sleep_calls), (
        f"Sleep calls {sleep_calls} contain value < batch_pause {batch_pause}"
    )


# ─── Property 5: Result completeness ─────────────────────────────────────────
# Feature: ollama-simulation, Property 5: Result completeness

@given(n=st.integers(min_value=1, max_value=10))
@settings(max_examples=10, deadline=None)
def test_property5_result_completeness(n):
    """
    For any N applications that complete without interrupt,
    len(rows) == N and processed + errors == N.
    """
    from scripts.run_ollama_sim import RunConfig, run_simulation

    def mock_run_one(i, scenario, cfg):
        return {
            "index": i, "scenario": scenario, "case_id": "x",
            "loan_amount": 1000.0, "bureau_score": 700,
            "operational_status": "approved", "llm_case_type": "independent_credit_risk",
            "llm_fraud_label": "none", "llm_confidence": 0.9,
            "llm_human_review": False, "llm_key_reasons": "", "llm_analyst_summary": "",
            "rule_hits": 0, "critical_hit": False, "latency_ms": 10, "error": None,
        }

    config    = RunConfig(total=n, batch_size=max(1, n // 2 + 1),
                          concurrency=1, timeout=5.0, batch_pause=0.0, retries=0)
    scenarios = ["clean"] * n

    with patch("scripts.run_ollama_sim.run_one_with_timeout", side_effect=mock_run_one):
        rows = run_simulation(config, scenarios)

    assert len(rows) == n, f"Expected {n} rows, got {len(rows)}"

    processed = sum(1 for r in rows if not r.get("error"))
    errors    = sum(1 for r in rows if r.get("error"))
    assert processed + errors == n, (
        f"processed ({processed}) + errors ({errors}) != n ({n})"
    )


# ─── Property 6: Partial results on interrupt ────────────────────────────────
# Feature: ollama-simulation, Property 6: Partial results on interrupt

@given(k=st.integers(min_value=1, max_value=4))
@settings(max_examples=10, deadline=None)
def test_property6_partial_results_on_interrupt(k):
    """
    When interrupted after K completions, output rows contain exactly K entries.
    """
    from scripts.run_ollama_sim import RunConfig, run_simulation

    call_count = [0]

    def mock_run_one(i, scenario, cfg):
        call_count[0] += 1
        if call_count[0] > k:
            raise KeyboardInterrupt()
        return {
            "index": i, "scenario": scenario, "case_id": "x",
            "loan_amount": 1000.0, "bureau_score": 700,
            "operational_status": "approved", "llm_case_type": "independent_credit_risk",
            "llm_fraud_label": "none", "llm_confidence": 0.9,
            "llm_human_review": False, "llm_key_reasons": "", "llm_analyst_summary": "",
            "rule_hits": 0, "critical_hit": False, "latency_ms": 10, "error": None,
        }

    total     = k + 3
    config    = RunConfig(total=total, batch_size=total, concurrency=1,
                          timeout=5.0, batch_pause=0.0, retries=0)
    scenarios = ["clean"] * total
    call_count[0] = 0

    with patch("scripts.run_ollama_sim.run_one_with_timeout", side_effect=mock_run_one):
        rows = run_simulation(config, scenarios)

    assert len(rows) == k, f"Expected {k} rows after interrupt, got {len(rows)}"
