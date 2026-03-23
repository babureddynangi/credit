# Implementation Plan: Ollama Simulation Runner

## Overview

Build `scripts/run_ollama_sim.py` — a resource-controlled replacement for `run_ollama_sample.py` that adds per-call timeouts, concurrency limits, inter-batch pauses, and bounded retries. Reuses all existing pipeline objects unchanged.

## Tasks

- [x] 1. Create RunConfig and CLI argument parsing
  - Define `RunConfig` dataclass with fields: `total`, `batch_size`, `timeout`, `concurrency`, `batch_pause`, `retries`
  - Add `parse_args()` using `argparse` with defaults matching requirements
  - Add `validate_config(config)` that calls `sys.exit(1)` with a descriptive message for invalid values (`total < 1`, `batch_size < 1`, `batch_size > total`)
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 3.1, 4.1, 5.1_

- [x] 1.1 Write property test for config validation
  - **Property 7: Config validation rejects bad inputs**
  - Use `hypothesis` to generate invalid `(total, batch_size)` combos and assert `validate_config` raises `SystemExit`
  - **Validates: Requirements 1.3, 1.4**

- [x] 2. Implement ollama_classify_with_retry
  - Port the `ollama_classify` function from `run_ollama_sample.py` into the new script
  - Wrap with `tenacity.retry` using `stop_after_attempt(retries + 1)`, `wait_exponential(min=1, max=10)`, retrying on `json.JSONDecodeError` and `openai.APIConnectionError`
  - Accept `config` as a parameter so retry count is dynamic
  - _Requirements: 5.1, 5.2, 5.3, 5.4_

- [x] 2.1 Write property test for retry count bound
  - **Property 4: Retry count bound**
  - Use `hypothesis` to generate retry counts 0–5; mock LLM to always raise `json.JSONDecodeError`; assert total call attempts == retries + 1
  - **Validates: Requirements 5.1, 5.2**

- [x] 3. Implement run_one_with_timeout
  - Build the full per-application pipeline: `build_intake` → `build_graph_risk` → `heuristic_scores` → `build_bundle` → `rule_engine.evaluate` → `ollama_classify_with_retry` → `policy_agent.apply`
  - Enforce timeout by wrapping the LLM call in a `concurrent.futures.Future` and calling `.result(timeout=config.timeout)`
  - Return a result dict on success; return `{"error": "timeout", ...}` on `TimeoutError`; return `{"error": "llm_error", ...}` on exhausted retries
  - Log timeout events to stdout with application index and elapsed time
  - _Requirements: 2.2, 2.3, 2.4_

- [x] 3.1 Write property test for timeout bound
  - **Property 1: Timeout bound**
  - Use `hypothesis` to generate timeout values 0.05–2.0s; mock LLM to sleep longer than timeout; assert result has `error="timeout"` and wall-clock time ≤ timeout + 1s
  - **Validates: Requirements 2.2, 2.3**

- [x] 4. Implement SimulationRunner batch loop
  - Use `concurrent.futures.ThreadPoolExecutor(max_workers=config.concurrency)`
  - Split applications into batches of `config.batch_size`
  - Submit each batch, collect results with `as_completed`, aggregate into counters
  - After each batch: print progress line (processed / total, elapsed, throughput, errors); sleep `config.batch_pause` if not the last batch
  - Wrap the outer loop in `try/except KeyboardInterrupt` to drain in-flight futures and break
  - _Requirements: 3.1, 3.2, 4.1, 4.2, 4.3, 6.1, 7.1, 7.2_

- [x] 4.1 Write property test for concurrency cap
  - **Property 2: Concurrency cap**
  - Use `hypothesis` to generate concurrency values 1–4 and total 4–20; instrument mock LLM with a threading counter; assert peak concurrent calls ≤ concurrency
  - **Validates: Requirements 3.1, 3.2**

- [x] 4.2 Write property test for batch pause
  - **Property 3: Batch pause lower bound**
  - Mock `time.sleep`; generate batch_pause values 0.5–3.0 and multi-batch runs; assert `time.sleep` is called with value ≥ batch_pause between batches
  - **Validates: Requirements 4.1, 4.2**

- [x] 5. Implement write_results
  - Accept `rows` list and `summary` dict; write `results/ollama_run_summary.json` and `results/ollama_run_report.txt`
  - Summary JSON must include: `processed`, `llm_errors`, `timeout_errors`, `elapsed_seconds`, `operational_status`, `fraud_label`, `total_loan_volume`
  - Report TXT must match the format of the existing `run_ollama_sample.py` report
  - _Requirements: 6.2, 6.3_

- [x] 5.1 Write property test for result completeness
  - **Property 5: Result completeness**
  - Use `hypothesis` to generate N (1–30) with a mock LLM; run the full loop; assert `len(rows) == N` and `processed + errors == N`
  - **Validates: Requirements 6.2, 6.3**

- [x] 5.2 Write property test for partial results on interrupt
  - **Property 6: Partial results on interrupt**
  - Simulate interrupt after K completions using a mock that raises `KeyboardInterrupt` on the K+1th call; assert output files contain exactly K entries
  - **Validates: Requirements 7.1, 7.3**

- [x] 6. Checkpoint — Ensure all tests pass
  - Run `pytest tests/test_ollama_sim.py tests/test_ollama_sim_properties.py -v`
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- All property tests use `@settings(max_examples=100)` and are tagged with `# Feature: ollama-simulation, Property N: ...`
- The new script lives at `scripts/run_ollama_sim.py`; the old `run_ollama_sample.py` is left untouched
