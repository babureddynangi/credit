# Design Document: Ollama Simulation Runner

## Overview

Replace the hanging `scripts/run_ollama_sample.py` with a resource-controlled simulation runner (`scripts/run_ollama_sim.py`) that processes synthetic loan applications through the real Ollama llama3.2 model without overwhelming the local machine.

The core problem with the existing script: it fires LLM calls sequentially with no timeout, no retry cap, and no breathing room between calls. On a constrained machine, Ollama queues up, memory climbs, and the process stalls.

The new runner adds four levers: a per-call timeout, a concurrency cap, an inter-batch pause, and bounded retries — all configurable via CLI flags with safe defaults.

## Architecture

```
CLI args (argparse)
       │
       ▼
  RunConfig (dataclass)
       │
       ▼
  SimulationRunner
  ├── build_pipeline_inputs()   — generates synthetic intake/graph/scores/bundle
  ├── run_batch()               — submits batch to ThreadPoolExecutor
  │     └── run_one_with_timeout()
  │           ├── build_bundle + rule_engine.evaluate()
  │           ├── ollama_classify()  ← tenacity retry wrapper
  │           └── policy_agent.apply()
  ├── progress_reporter()       — prints after each batch
  └── write_results()           — JSON + TXT output
```

The existing pipeline objects (`RuleEngine`, `PolicyMappingAgent`, `LLMSummaryAgent`) are reused unchanged. The new script only adds the concurrency/timeout/retry shell around the existing `ollama_classify` logic.

## Components and Interfaces

### RunConfig

```python
@dataclass
class RunConfig:
    total: int = 10
    batch_size: int = 5
    timeout: float = 30.0
    concurrency: int = 1
    batch_pause: float = 2.0
    retries: int = 2
```

Validated at startup; invalid values print a message and `sys.exit(1)`.

### ollama_classify_with_retry(bundle, config)

Wraps the existing `ollama_classify` logic from `run_ollama_sample.py` with a `tenacity` retry decorator:

```python
@tenacity.retry(
    stop=tenacity.stop_after_attempt(config.retries + 1),
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
    retry=tenacity.retry_if_exception_type((json.JSONDecodeError, httpx.RequestError, openai.APIConnectionError)),
    reraise=True,
)
def _call(bundle): ...
```

### run_one_with_timeout(i, scenario, config)

Runs the full per-application pipeline. Called inside a `ThreadPoolExecutor` worker. Returns a result dict or an error dict with `{"error": "timeout"|"llm_error", ...}`.

Timeout is enforced by submitting the blocking LLM call to a separate `concurrent.futures.Future` and calling `.result(timeout=config.timeout)`.

### SimulationRunner.run()

Main loop:

```python
for batch in chunks(range(1, total+1), batch_size):
    futures = {executor.submit(run_one_with_timeout, i, scenarios[i-1], config): i for i in batch}
    for f in as_completed(futures):
        result = f.result()
        aggregate(result)
    progress_report()
    if not last_batch:
        time.sleep(config.batch_pause)
```

`KeyboardInterrupt` is caught at the top level; partial results are written before exit.

### write_results(rows, summary, config)

Writes `results/ollama_run_summary.json` and `results/ollama_run_report.txt` in the same schema as the existing script so downstream tooling is unaffected.

## Data Models

No new Pydantic models are needed. The runner reuses all existing models from `api/schemas/models.py`.

Internal result dict per application:

```python
{
    "index": int,
    "scenario": str,
    "case_id": str,
    "loan_amount": float,
    "bureau_score": int,
    "operational_status": str,
    "llm_case_type": str,
    "llm_fraud_label": str,
    "llm_confidence": float,
    "llm_human_review": bool,
    "llm_key_reasons": str,
    "llm_analyst_summary": str,
    "rule_hits": int,
    "critical_hit": bool,
    "latency_ms": int,
    "error": str | None,   # "timeout" | "llm_error" | None
}
```

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

Property 1: Timeout bound
*For any* application processed with a configured timeout T, the wall-clock time spent on that application's LLM call must not exceed T + a small scheduling overhead (≤ 1s).
**Validates: Requirements 2.1, 2.2**

Property 2: Concurrency cap
*For any* run with concurrency limit C, the number of simultaneously in-flight LLM calls at any point must not exceed C.
**Validates: Requirements 3.1, 3.2, 3.3**

Property 3: Batch pause lower bound
*For any* run with batch_pause P > 0, the elapsed time between the last result of batch N and the first dispatch of batch N+1 must be ≥ P seconds.
**Validates: Requirements 4.1, 4.2**

Property 4: Retry count bound
*For any* application that encounters a retryable error, the total number of LLM call attempts must not exceed retries + 1.
**Validates: Requirements 5.1, 5.2, 5.3**

Property 5: Result completeness
*For any* run of N applications that completes without interrupt, the output summary must contain exactly N entries (successes + errors), and the processed count plus error count must equal N.
**Validates: Requirements 6.1, 6.2, 6.3**

Property 6: Partial results on interrupt
*For any* run interrupted after K applications have completed, the written summary must contain exactly K entries and the report must reflect only those K applications.
**Validates: Requirements 7.1, 7.2, 7.3**

Property 7: Config validation rejects bad inputs
*For any* combination of `--total < 1`, `--batch-size < 1`, or `--batch-size > --total`, the runner must exit with code 1 and a non-empty error message without processing any applications.
**Validates: Requirements 1.3, 1.4**

## Error Handling

| Situation | Behaviour |
|---|---|
| LLM call times out | Record `error="timeout"`, log index + elapsed, continue |
| LLM returns malformed JSON (all retries exhausted) | Record `error="llm_error"`, continue |
| Network error (all retries exhausted) | Record `error="llm_error"`, continue |
| KeyboardInterrupt | Drain in-flight futures, write partial results, exit 0 |
| Invalid CLI args | Print descriptive message, exit 1 |
| Ollama not running | First call fails → retried → recorded as error; run continues |

## Testing Strategy

### Unit tests (`tests/test_ollama_sim.py`)

- Config validation: bad `--total`, bad `--batch-size`, bad `--batch-size > --total`
- Result aggregation: correct counts when mixing success/timeout/error rows
- `write_results`: output files contain expected keys

### Property-based tests (`tests/test_ollama_sim_properties.py`)

Uses `hypothesis` (already in the project via `.hypothesis/` directory).

- **Property 4 (retry bound)**: Generate random retry counts (0–5) and a mock that always raises. Assert call count ≤ retries + 1.
- **Property 5 (result completeness)**: Generate random N (1–50) with a mock LLM. Assert `len(rows) == N`.
- **Property 7 (config validation)**: Generate invalid config combos. Assert exit code 1.

Property tests run with `@settings(max_examples=100)`.

Each test is tagged:
```python
# Feature: ollama-simulation, Property N: <property text>
```

### Manual smoke test

```bash
python scripts/run_ollama_sim.py --total 3 --batch-size 1 --timeout 20 --concurrency 1 --batch-pause 1
```

Expected: 3 applications processed, no hang, results written to `results/`.
